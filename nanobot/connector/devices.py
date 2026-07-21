"""Device pairing and token lifecycle for the nanobot Connector.

Two-stage authentication (design decision D5):

1. A logged-in WebUI user mints a short-lived, one-time **pairing code**.
2. The connector redeems that code for a long-lived **device token**. The server
   stores only ``sha256(token)`` — the plaintext is returned exactly once.

The store is workspace-scoped (``<workspace>/connector/devices.json``) and written
atomically. It is deliberately private-assistant scale: a small JSON file guarded
by a threading lock so it is callable from both sync CLI and async HTTP handlers.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import string
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.utils.helpers import _write_text_atomic

_ALPHABET = string.ascii_uppercase + string.digits
_CODE_LENGTH = 8


def _now() -> float:
    return time.time()


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


@dataclass
class Device:
    node_id: str
    name: str
    platform: str
    owner_id: str
    machine_fingerprint: str
    token_sha256: str
    created_at: str
    last_seen_at: str | None = None
    revoked: bool = False
    alias: str = ""  # owner-set readable name for disambiguating devices

    def public(self) -> dict[str, Any]:
        """Serialize for management API — never leaks the token hash."""
        return {
            "nodeId": self.node_id,
            "name": self.name,
            "alias": self.alias,
            "platform": self.platform,
            "ownerId": self.owner_id,
            "createdAt": self.created_at,
            "lastSeenAt": self.last_seen_at,
            "revoked": self.revoked,
        }


class DeviceStore:
    """Pairing codes + device tokens, persisted to ``devices.json``."""

    def __init__(self, path: Path, *, pairing_ttl_s: int = 600) -> None:
        self._path = Path(path)
        self._pairing_ttl_s = pairing_ttl_s
        self._lock = threading.RLock()
        self._devices: dict[str, Device] = {}
        self._pending: dict[str, dict[str, Any]] = {}  # code -> {ownerId, expiresAt}
        self._load()

    # -- persistence --------------------------------------------------------

    def _load(self) -> None:
        try:
            import json

            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (ValueError, OSError):
            logger.warning("Corrupted connector device store, resetting: {}", self._path)
            return
        for row in data.get("devices", []):
            try:
                device = Device(
                    node_id=row["nodeId"],
                    name=row.get("name", "unknown"),
                    platform=row.get("platform", "unknown"),
                    owner_id=row.get("ownerId", ""),
                    machine_fingerprint=row.get("machineFingerprint", ""),
                    token_sha256=row["tokenSha256"],
                    created_at=row.get("createdAt", _iso(_now())),
                    last_seen_at=row.get("lastSeenAt"),
                    revoked=bool(row.get("revoked", False)),
                    alias=row.get("alias", ""),
                )
            except KeyError:
                continue
            self._devices[device.node_id] = device

    def _save(self) -> None:
        import json

        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "devices": [
                {
                    "nodeId": d.node_id,
                    "name": d.name,
                    "platform": d.platform,
                    "ownerId": d.owner_id,
                    "machineFingerprint": d.machine_fingerprint,
                    "tokenSha256": d.token_sha256,
                    "createdAt": d.created_at,
                    "lastSeenAt": d.last_seen_at,
                    "revoked": d.revoked,
                    "alias": d.alias,
                }
                for d in self._devices.values()
            ]
        }
        _write_text_atomic(self._path, json.dumps(payload, indent=2, ensure_ascii=False))

    def _gc_pending(self) -> None:
        now = _now()
        expired = [c for c, info in self._pending.items() if info.get("expiresAt", 0) < now]
        for code in expired:
            del self._pending[code]

    # -- pairing codes ------------------------------------------------------

    def generate_pairing_code(self, owner_id: str) -> tuple[str, float]:
        """Mint a one-time pairing code. Returns ``(code, expires_at_epoch)``."""
        with self._lock:
            self._gc_pending()
            code = "".join(secrets.choice(_ALPHABET) for _ in range(_CODE_LENGTH))
            expires_at = _now() + self._pairing_ttl_s
            self._pending[code] = {"ownerId": owner_id, "expiresAt": expires_at}
            logger.info("connector: generated pairing code for owner {}", owner_id)
            return code, expires_at

    def invalidate_all_pending(self) -> int:
        """Void every unredeemed pairing code (circuit breaker). Returns count."""
        with self._lock:
            count = len(self._pending)
            self._pending.clear()
            return count

    def _match_pending(self, code: str) -> str | None:
        """Constant-time match of *code* against pending entries. Returns the key."""
        self._gc_pending()
        for candidate in self._pending:
            if hmac.compare_digest(candidate, code):
                return candidate
        return None

    # -- redemption ---------------------------------------------------------

    def redeem_pairing_code(
        self,
        code: str,
        *,
        name: str,
        platform: str,
        machine_fingerprint: str,
    ) -> tuple[str, str] | None:
        """Redeem *code* for a device token.

        Returns ``(node_id, plaintext_token)`` on success or ``None`` if the code
        is unknown/expired. Same-fingerprint re-pairing replaces the old record
        and revokes its token (no zombie devices).
        """
        with self._lock:
            matched = self._match_pending(code)
            if matched is None:
                return None
            owner_id = self._pending.pop(matched)["ownerId"]

            token = secrets.token_urlsafe(32)
            token_hash = _sha256_hex(token)
            now = _iso(_now())

            existing = self._find_by_fingerprint(owner_id, machine_fingerprint)
            if existing is not None:
                existing.token_sha256 = token_hash
                existing.name = name
                existing.platform = platform
                existing.revoked = False
                existing.last_seen_at = None
                node_id = existing.node_id
                logger.info("connector: re-paired existing device {}", node_id)
            else:
                node_id = f"dev-{secrets.token_hex(6)}"
                self._devices[node_id] = Device(
                    node_id=node_id,
                    name=name,
                    platform=platform,
                    owner_id=owner_id,
                    machine_fingerprint=machine_fingerprint,
                    token_sha256=token_hash,
                    created_at=now,
                )
                logger.info("connector: paired new device {} for owner {}", node_id, owner_id)
            self._save()
            return node_id, token

    def _find_by_fingerprint(self, owner_id: str, fingerprint: str) -> Device | None:
        if not fingerprint:
            return None
        for device in self._devices.values():
            if device.owner_id == owner_id and device.machine_fingerprint == fingerprint:
                return device
        return None

    # -- token verification -------------------------------------------------

    def verify_token(self, token: str) -> Device | None:
        """Return the (non-revoked) device owning *token*, else ``None``.

        Uses constant-time comparison against stored hashes.
        """
        if not token:
            return None
        candidate = _sha256_hex(token)
        with self._lock:
            for device in self._devices.values():
                if device.revoked:
                    continue
                if hmac.compare_digest(device.token_sha256, candidate):
                    return device
        return None

    # -- management ---------------------------------------------------------

    def get(self, node_id: str) -> Device | None:
        with self._lock:
            return self._devices.get(node_id)

    def list_devices(self, *, owner_id: str | None = None) -> list[Device]:
        with self._lock:
            devices = [d for d in self._devices.values() if not d.revoked]
        if owner_id is None:
            return devices
        return [d for d in devices if d.owner_id == owner_id]

    def revoke(self, node_id: str, *, owner_id: str | None = None) -> bool:
        """Mark a device revoked. If *owner_id* is given, enforce ownership."""
        with self._lock:
            device = self._devices.get(node_id)
            if device is None or device.revoked:
                return False
            if owner_id is not None and device.owner_id != owner_id:
                return False
            device.revoked = True
            self._save()
            logger.info("connector: revoked device {}", node_id)
            return True

    def touch_last_seen(self, node_id: str) -> None:
        with self._lock:
            device = self._devices.get(node_id)
            if device is None:
                return
            device.last_seen_at = _iso(_now())
            self._save()

    def set_alias(self, node_id: str, alias: str, *, owner_id: str | None = None) -> bool:
        """Set an owner-facing readable name; enforces ownership when given."""
        with self._lock:
            device = self._devices.get(node_id)
            if device is None or device.revoked:
                return False
            if owner_id is not None and device.owner_id != owner_id:
                return False
            device.alias = alias.strip()[:128]
            self._save()
            return True


@dataclass
class _IpState:
    failures: int = 0
    locked_until: float = 0.0


class PairRateLimiter:
    """Brute-force protection for the unauthenticated ``/connector/pair`` endpoint.

    - Per-source-IP lockout after ``max_failures`` failures for ``lockout_s``.
    - Global circuit breaker: when failures across all IPs exceed
      ``global_threshold`` within ``global_window_s``, the caller is told to void
      all unredeemed pairing codes and raise an alert.
    """

    def __init__(
        self,
        *,
        max_failures: int = 5,
        lockout_s: int = 900,
        global_threshold: int = 50,
        global_window_s: int = 300,
    ) -> None:
        self.max_failures = max_failures
        self.lockout_s = lockout_s
        self.global_threshold = global_threshold
        self.global_window_s = global_window_s
        self._lock = threading.Lock()
        self._ips: dict[str, _IpState] = {}
        self._global_failures: list[float] = []

    def is_locked(self, ip: str) -> bool:
        with self._lock:
            state = self._ips.get(ip)
            if state is None:
                return False
            if state.locked_until and state.locked_until > _now():
                return True
            if state.locked_until and state.locked_until <= _now():
                # lock expired: reset
                self._ips.pop(ip, None)
            return False

    def record_success(self, ip: str) -> None:
        with self._lock:
            self._ips.pop(ip, None)

    def record_failure(self, ip: str) -> bool:
        """Record a failed attempt. Returns ``True`` if the global breaker tripped."""
        now = _now()
        with self._lock:
            state = self._ips.setdefault(ip, _IpState())
            state.failures += 1
            if state.failures >= self.max_failures:
                state.locked_until = now + self.lockout_s
                logger.warning("connector: locked pairing source {} for {}s", ip, self.lockout_s)

            cutoff = now - self.global_window_s
            self._global_failures = [t for t in self._global_failures if t >= cutoff]
            self._global_failures.append(now)
            tripped = len(self._global_failures) >= self.global_threshold
            if tripped:
                logger.warning(
                    "connector: global pairing failure breaker tripped ({} failures)",
                    len(self._global_failures),
                )
                self._global_failures.clear()
            return tripped
