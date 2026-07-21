"""Per-device, per-tool authorization for controlled execution (add-connector-local-tools).

Separates two roles:

- **operator**: the session/user driving the nanobot server that issues a tool call.
- **device owner**: the account that paired the device (``Device.owner_id``).

Self-use (operator == owner) is authorized implicitly. Cross-person use requires the
owner to explicitly grant an operator a specific tool (optionally time-limited). The
owner can revoke any grant instantly and sees every active grant + pending request —
this is the "device owner sovereignty" principle. An unauthorized call is reported as
"not found" upstream so tool existence never leaks across operators.

Persisted alongside the device store (``<workspace>/connector/grants.json``) with the
same atomic-write + lock discipline.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.utils.helpers import _write_text_atomic


def _now() -> float:
    return time.time()


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


@dataclass
class Grant:
    node_id: str
    tool: str
    operator_id: str
    granted_by: str
    created_at: str
    expires_at: float | None = None  # epoch; None = no expiry

    def active(self, *, now: float | None = None) -> bool:
        if self.expires_at is None:
            return True
        return (now or _now()) < self.expires_at

    def public(self) -> dict[str, Any]:
        return {
            "nodeId": self.node_id,
            "tool": self.tool,
            "operatorId": self.operator_id,
            "grantedBy": self.granted_by,
            "createdAt": self.created_at,
            "expiresAt": int(self.expires_at) if self.expires_at is not None else None,
        }


@dataclass
class AccessRequest:
    node_id: str
    operator_id: str
    tools: list[str] = field(default_factory=list)
    reason: str = ""
    requested_at: str = ""

    def public(self) -> dict[str, Any]:
        return {
            "nodeId": self.node_id,
            "operatorId": self.operator_id,
            "tools": list(self.tools),
            "reason": self.reason,
            "requestedAt": self.requested_at,
        }


def _grant_key(node_id: str, tool: str, operator_id: str) -> str:
    return f"{node_id}\x1f{tool}\x1f{operator_id}"


class AuthorizationStore:
    """Cross-person tool grants + pending access requests, JSON-persisted."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = threading.RLock()
        self._grants: dict[str, Grant] = {}
        self._requests: dict[str, AccessRequest] = {}
        self._load()

    # -- persistence --------------------------------------------------------

    def _load(self) -> None:
        try:
            import json

            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (ValueError, OSError):
            logger.warning("Corrupted connector grants store, resetting: {}", self._path)
            return
        for row in data.get("grants", []):
            try:
                grant = Grant(
                    node_id=row["nodeId"],
                    tool=row["tool"],
                    operator_id=row["operatorId"],
                    granted_by=row.get("grantedBy", ""),
                    created_at=row.get("createdAt", _iso(_now())),
                    expires_at=row.get("expiresAt"),
                )
            except KeyError:
                continue
            self._grants[_grant_key(grant.node_id, grant.tool, grant.operator_id)] = grant
        for row in data.get("requests", []):
            try:
                req = AccessRequest(
                    node_id=row["nodeId"],
                    operator_id=row["operatorId"],
                    tools=list(row.get("tools", [])),
                    reason=row.get("reason", ""),
                    requested_at=row.get("requestedAt", _iso(_now())),
                )
            except KeyError:
                continue
            self._requests[f"{req.node_id}\x1f{req.operator_id}"] = req

    def _save(self) -> None:
        import json

        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "grants": [g.public() for g in self._grants.values()],
            "requests": [r.public() for r in self._requests.values()],
        }
        _write_text_atomic(self._path, json.dumps(payload, indent=2, ensure_ascii=False))

    # -- authorization check -----------------------------------------------

    def is_authorized(
        self, node_id: str, tool: str, *, operator_id: str, owner_id: str
    ) -> bool:
        """True if *operator_id* may call *tool* on *node_id*.

        Self-use (operator == owner) is always authorized. Otherwise an active,
        non-expired grant is required. Expired grants are garbage-collected.
        """
        if operator_id == owner_id:
            return True
        with self._lock:
            grant = self._grants.get(_grant_key(node_id, tool, operator_id))
            if grant is None:
                return False
            if not grant.active():
                del self._grants[_grant_key(node_id, tool, operator_id)]
                self._save()
                return False
            return True

    # -- owner operations (sovereignty) ------------------------------------

    def grant(
        self,
        node_id: str,
        tool: str,
        operator_id: str,
        *,
        granted_by: str,
        ttl_s: int | None = None,
    ) -> Grant:
        with self._lock:
            expires_at = (_now() + ttl_s) if ttl_s else None
            grant = Grant(
                node_id=node_id,
                tool=tool,
                operator_id=operator_id,
                granted_by=granted_by,
                created_at=_iso(_now()),
                expires_at=expires_at,
            )
            self._grants[_grant_key(node_id, tool, operator_id)] = grant
            # Clearing any matching request: the owner has acted on it.
            self._requests.pop(f"{node_id}\x1f{operator_id}", None)
            self._save()
            logger.info(
                "connector: granted {} tool {} on {} (by {})",
                operator_id, tool, node_id, granted_by,
            )
            return grant

    def revoke(self, node_id: str, tool: str, operator_id: str) -> bool:
        with self._lock:
            key = _grant_key(node_id, tool, operator_id)
            if key not in self._grants:
                return False
            del self._grants[key]
            self._save()
            logger.info("connector: revoked {} tool {} on {}", operator_id, tool, node_id)
            return True

    def list_grants(self, *, node_id: str | None = None) -> list[Grant]:
        with self._lock:
            grants = [g for g in self._grants.values() if g.active()]
        if node_id is None:
            return grants
        return [g for g in grants if g.node_id == node_id]

    def active_operators(self, node_id: str) -> list[dict[str, Any]]:
        """Who currently has access to *node_id*, and to which tools."""
        by_operator: dict[str, list[str]] = {}
        for g in self.list_grants(node_id=node_id):
            by_operator.setdefault(g.operator_id, []).append(g.tool)
        return [
            {"operatorId": op, "tools": sorted(tools)} for op, tools in by_operator.items()
        ]

    # -- cross-person access requests --------------------------------------

    def request_access(
        self, node_id: str, operator_id: str, *, tools: list[str], reason: str = ""
    ) -> AccessRequest:
        with self._lock:
            req = AccessRequest(
                node_id=node_id,
                operator_id=operator_id,
                tools=list(tools),
                reason=reason,
                requested_at=_iso(_now()),
            )
            self._requests[f"{node_id}\x1f{operator_id}"] = req
            self._save()
            return req

    def list_requests(self, *, node_id: str | None = None) -> list[AccessRequest]:
        with self._lock:
            requests = list(self._requests.values())
        if node_id is None:
            return requests
        return [r for r in requests if r.node_id == node_id]

    def deny_request(self, node_id: str, operator_id: str) -> bool:
        with self._lock:
            removed = self._requests.pop(f"{node_id}\x1f{operator_id}", None)
            if removed is None:
                return False
            self._save()
            return True
