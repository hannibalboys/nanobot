"""Server-side controlled desktop session orchestration (add-connector-desktop-control).

The security-critical half of desktop control. A session is a first-class,
time-boxed, human-in-the-loop object:

- **no ``auto``**: opening a session always triggers the device's on-device owner
  consent (enforced client-side); cross-person use additionally needs a grant.
- **bounded**: hard max duration + idle timeout; capture/act past the bound end it.
- **sensitive actions**: pay/confirm/delete/authorize clicks and password-like
  typing require a WebUI confirmation before injection (default-deny on timeout).
- **privacy**: frames are NOT persisted unless the owner turns on recording;
  recordings have a retention window and can be deleted; every action is audited
  with a reference to the frame that prompted it.
- **take over / terminate**: the owner can end a session instantly.

Frames flow through memory only; the manager never writes a frame to disk unless
the session was explicitly opened with ``record=True``.
"""

from __future__ import annotations

import base64
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.connector import protocol as proto
from nanobot.connector.authz import AuthorizationStore
from nanobot.connector.exec import ApprovalBroker
from nanobot.connector.hub import ConnectorError, ConnectorHub

# Authorization key for cross-person desktop control (per device).
DESKTOP_AUTHZ_KEY = "desktop:control"

# Heuristic sensitive-control patterns (design open question: heuristic first cut).
_SENSITIVE_LABEL_RE = re.compile(
    r"(pay|purchase|checkout|buy|confirm|delete|remove|authori[sz]e|approve|"
    r"支付|付款|购买|确认|删除|授权|同意)",
    re.IGNORECASE,
)


def is_sensitive_action(action: dict) -> bool:
    """True if an action needs explicit confirmation before injection.

    Sensitive when: the model marks it (``sensitive: true``); it types into a
    field flagged as a password/secret; or it clicks a control whose label matches
    pay/confirm/delete/authorize semantics.
    """
    if action.get("sensitive") is True:
        return True
    atype = action.get("type", "")
    if atype in ("type", "key") and (action.get("secret") or action.get("password")):
        return True
    label = str(action.get("label", "") or action.get("target_text", ""))
    if atype in ("click", "double_click", "right_click") and _SENSITIVE_LABEL_RE.search(label):
        return True
    return False


def _desktop_summary(action: dict) -> dict[str, Any]:
    """Audit-safe summary of a desktop action.

    Typed content (``text``) is masked to a length only — it can be a password,
    a private message, etc. — so the audit never persists what was typed.
    """
    out: dict[str, Any] = {}
    for k, v in (action or {}).items():
        if k == "type":
            continue
        if k == "text":
            out[k] = f"<{len(str(v))} chars>"
        else:
            s = str(v)
            out[k] = s if len(s) <= 40 else s[:40] + "…"
    return out


def audit_desktop(
    workspace_path: Path,
    *,
    session_id: str,
    node_id: str,
    operator_id: str | None,
    action_type: str,
    params_summary: dict[str, Any],
    sensitive: bool,
    confirmed: bool,
    result: str,
    frame_ref: str = "",
) -> None:
    """Append one per-action desktop audit record (never disableable)."""
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sessionId": session_id,
        "nodeId": node_id,
        "operatorId": operator_id or "",
        "action": action_type,
        "params": params_summary,
        "sensitive": sensitive,
        "confirmed": confirmed,
        "result": result,
        "frameRef": frame_ref,
    }
    logger.info(
        "connector desktop audit: session={} node={} action={} sensitive={} result={}",
        session_id, node_id, action_type, sensitive, result,
    )
    path = Path(workspace_path) / "connector" / "desktop-audit.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning("connector desktop audit write failed: {}", exc)


def cleanup_recordings(workspace_path: Path, *, retention_days: int) -> int:
    """Delete session recording dirs older than the retention window. Returns count."""
    base = Path(workspace_path) / "connector" / "desktop-recordings"
    if not base.exists():
        return 0
    cutoff = time.time() - retention_days * 86400
    removed = 0
    for session_dir in base.iterdir():
        try:
            if session_dir.is_dir() and session_dir.stat().st_mtime < cutoff:
                for f in session_dir.iterdir():
                    f.unlink(missing_ok=True)
                session_dir.rmdir()
                removed += 1
        except OSError:
            continue
    return removed


def read_desktop_audit(
    workspace_path: Path,
    *,
    node_ids: set[str] | None = None,
    session_id: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Return recent desktop per-action audit records (newest first), for replay.

    ``node_ids`` scopes to the owner's devices; ``session_id`` narrows to one session.
    """
    path = Path(workspace_path) / "connector" / "desktop-audit.log"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if node_ids is not None and record.get("nodeId") not in node_ids:
            continue
        if session_id is not None and record.get("sessionId") != session_id:
            continue
        out.append(record)
        if len(out) >= limit:
            break
    return out


def list_recordings(workspace_path: Path, *, node_ids: set[str] | None = None) -> list[dict[str, Any]]:
    """List recorded sessions (dir name = session id, frame count, mtime).

    A recording's node ownership is resolved from the audit log (the session's
    ``session.start`` record), so callers can scope by owned devices.
    """
    base = Path(workspace_path) / "connector" / "desktop-recordings"
    if not base.exists():
        return []
    # session -> node from the audit trail (best-effort)
    session_node: dict[str, str] = {}
    for rec in read_desktop_audit(workspace_path, limit=5000):
        session_node.setdefault(rec.get("sessionId", ""), rec.get("nodeId", ""))
    out: list[dict[str, Any]] = []
    for d in base.iterdir():
        if not d.is_dir():
            continue
        node_id = session_node.get(d.name, "")
        if node_ids is not None and node_id not in node_ids:
            continue
        try:
            frames = sum(1 for _ in d.iterdir())
            mtime = int(d.stat().st_mtime)
        except OSError:
            continue
        out.append({"sessionId": d.name, "nodeId": node_id, "frames": frames, "mtime": mtime})
    out.sort(key=lambda r: r["mtime"], reverse=True)
    return out


def delete_recording(workspace_path: Path, session_id: str) -> bool:
    """Delete one session's recording. Returns True if it existed."""
    base = Path(workspace_path) / "connector" / "desktop-recordings"
    session_dir = base / session_id
    # Guard against traversal via a crafted session id.
    if not session_id or "/" in session_id or "\\" in session_id or ".." in session_id:
        return False
    if not session_dir.is_dir():
        return False
    try:
        for f in session_dir.iterdir():
            f.unlink(missing_ok=True)
        session_dir.rmdir()
    except OSError:
        return False
    return True


@dataclass
class DesktopSession:
    session_id: str
    node_id: str
    operator_id: str
    owner_id: str
    goal: str
    started_at: float
    last_activity: float
    record: bool = False
    frame_seq: int = 0
    _ended: bool = field(default=False)


class DesktopSessionManager:
    """Owns all active desktop sessions and enforces the session security model."""

    def __init__(
        self,
        *,
        hub: ConnectorHub,
        authz: AuthorizationStore,
        workspace: Path,
        config: Any,
        broker: ApprovalBroker | None = None,
    ) -> None:
        self._hub = hub
        self._authz = authz
        self._workspace = Path(workspace)
        self._config = config
        self._broker = broker
        self._sessions: dict[str, DesktopSession] = {}

    # -- lifecycle ----------------------------------------------------------

    async def start(
        self, node_id: str, *, operator_id: str, owner_id: str, goal: str, record: bool = False
    ) -> dict[str, Any]:
        """Open a controlled session (device enforces on-device owner consent).

        Returns ``{sessionId, image, width, height}`` with the first frame.
        """
        # Cross-person requires an explicit device grant; unauthorized is hidden.
        if operator_id != owner_id and not self._authz.is_authorized(
            node_id, DESKTOP_AUTHZ_KEY, operator_id=operator_id, owner_id=owner_id
        ):
            raise ConnectorError(proto.ERROR_DESKTOP_UNSUPPORTED, "desktop control not available")

        # opportunistic retention cleanup
        cleanup_recordings(self._workspace, retention_days=self._config.desktop_recording_retention_days)

        session_id = uuid.uuid4().hex
        # The device's on_local_authorize gate runs here; denial → session_ended.
        # Server capture caps ride along so device policy honors server policy too.
        await self._hub.desktop_rpc(
            node_id, "desktop.session.start",
            {
                "sessionId": session_id, "operator": operator_id, "goal": goal,
                "maxDimension": self._config.desktop_max_dimension,
                "maxFps": self._config.desktop_max_fps,
            },
            timeout=self._config.approval_ttl_s, owner_id=owner_id,
        )
        now = time.monotonic()
        session = DesktopSession(
            session_id=session_id, node_id=node_id, operator_id=operator_id,
            owner_id=owner_id, goal=goal, started_at=now, last_activity=now, record=record,
        )
        self._sessions[session_id] = session
        audit_desktop(
            self._workspace, session_id=session_id, node_id=node_id, operator_id=operator_id,
            action_type="session.start", params_summary={"goal": goal[:80]},
            sensitive=False, confirmed=True, result="ok",
        )
        try:
            frame = await self._capture(session)
        except ConnectorError:
            # First capture failed — don't leave an orphaned session open on the
            # device or in our registry.
            await self._end_session(session, reason="start capture failed")
            raise
        return {"sessionId": session_id, **frame}

    async def capture(self, session_id: str, *, operator_id: str) -> dict[str, Any]:
        session = self._session_for(session_id, operator_id)
        await self._enforce_active(session)
        return await self._capture(session)

    async def act(self, session_id: str, action: dict, *, operator_id: str) -> dict[str, Any]:
        """Inject one action (after sensitive-action confirmation), return next frame."""
        session = self._session_for(session_id, operator_id)
        await self._enforce_active(session)

        atype = action.get("type", "")
        if atype not in proto.DESKTOP_ACTIONS:
            raise ConnectorError(proto.ERROR_OUT_OF_BOUNDS, f"unknown action type: {atype}")

        sensitive = is_sensitive_action(action)
        summary = _desktop_summary(action)
        frame_ref = f"{session_id}:{session.frame_seq}"
        if sensitive:
            confirmed = await self._confirm_sensitive(session, atype, summary)
            if not confirmed:
                audit_desktop(
                    self._workspace, session_id=session_id, node_id=session.node_id,
                    operator_id=operator_id, action_type=atype, params_summary=summary,
                    sensitive=True, confirmed=False, result="blocked", frame_ref=frame_ref,
                )
                raise ConnectorError(
                    proto.ERROR_SENSITIVE_UNCONFIRMED, "sensitive action was not confirmed"
                )

        try:
            await self._hub.desktop_rpc(
                session.node_id, "desktop.input", {"sessionId": session_id, "action": action},
                timeout=self._config.rpc_timeout_s, owner_id=session.owner_id,
            )
        except ConnectorError as exc:
            audit_desktop(
                self._workspace, session_id=session_id, node_id=session.node_id,
                operator_id=operator_id, action_type=atype, params_summary=summary,
                sensitive=sensitive, confirmed=sensitive, result=exc.code, frame_ref=frame_ref,
            )
            raise
        audit_desktop(
            self._workspace, session_id=session_id, node_id=session.node_id,
            operator_id=operator_id, action_type=atype, params_summary=summary,
            sensitive=sensitive, confirmed=sensitive, result="ok", frame_ref=frame_ref,
        )
        session.last_activity = time.monotonic()
        return await self._capture(session)

    async def end(self, session_id: str, *, operator_id: str) -> dict[str, Any]:
        session = self._sessions.get(session_id)
        if session is None:
            return {"ended": True}
        await self._end_session(session, reason="ended")
        return {"ended": True}

    async def take_over(self, session_id: str, *, owner_id: str) -> bool:
        """Owner takes over / terminates a session immediately."""
        session = self._sessions.get(session_id)
        if session is None or session.owner_id != owner_id:
            return False
        await self._end_session(session, reason="taken_over")
        return True

    def list_sessions(self, *, owner_id: str) -> list[dict[str, Any]]:
        # Drop sessions whose device has disconnected — the device ends its own
        # session on disconnect, so those server records are stale phantoms.
        online = {n["nodeId"] for n in self._hub.list_nodes()}
        for sid in [sid for sid, s in self._sessions.items() if s.node_id not in online]:
            self._sessions.pop(sid, None)
        return [
            {
                "sessionId": s.session_id, "nodeId": s.node_id, "operatorId": s.operator_id,
                "goal": s.goal, "recording": s.record,
                "ageS": int(time.monotonic() - s.started_at),
            }
            for s in self._sessions.values()
            if s.owner_id == owner_id
        ]

    # -- internals ----------------------------------------------------------

    async def _capture(self, session: DesktopSession) -> dict[str, Any]:
        result = await self._hub.desktop_rpc(
            session.node_id, "desktop.capture", {"sessionId": session.session_id},
            timeout=self._config.rpc_timeout_s, owner_id=session.owner_id,
        )
        session.last_activity = time.monotonic()
        session.frame_seq += 1
        if session.record:
            self._persist_frame(session, result)
        return {
            "image": result.get("image", ""), "width": result.get("width", 0),
            "height": result.get("height", 0), "format": result.get("format", "png"),
        }

    def _persist_frame(self, session: DesktopSession, frame: dict) -> None:
        """Only called when the owner opted into recording for this session."""
        image_b64 = frame.get("image", "")
        if not image_b64:
            return
        rec_dir = self._workspace / "connector" / "desktop-recordings" / session.session_id
        try:
            rec_dir.mkdir(parents=True, exist_ok=True)
            (rec_dir / f"{session.frame_seq:06d}.{frame.get('format', 'png')}").write_bytes(
                base64.b64decode(image_b64)
            )
        except (OSError, ValueError) as exc:
            logger.warning("desktop recording write failed: {}", exc)

    def _session_for(self, session_id: str, operator_id: str) -> DesktopSession:
        session = self._sessions.get(session_id)
        if session is None or session._ended:
            raise ConnectorError(proto.ERROR_SESSION_ENDED, "session not active")
        if session.operator_id != operator_id:
            raise ConnectorError(proto.ERROR_SESSION_ENDED, "session belongs to another operator")
        return session

    async def _enforce_active(self, session: DesktopSession) -> None:
        """End (and tell the device to stop) if the session exceeded its bounds."""
        now = time.monotonic()
        why = ""
        if now - session.started_at > self._config.desktop_session_max_s:
            why = "max duration"
        elif now - session.last_activity > self._config.desktop_idle_timeout_s:
            why = "idle timeout"
        if why:
            # MUST stop capture/input on the device — not just drop the record.
            await self._end_session(session, reason=why)
            raise ConnectorError(proto.ERROR_SESSION_ENDED, f"session ended: {why}")

    async def _end_session(self, session: DesktopSession, *, reason: str) -> None:
        session._ended = True
        self._sessions.pop(session.session_id, None)
        try:
            await self._hub.desktop_rpc(
                session.node_id, "desktop.session.end", {"sessionId": session.session_id},
                timeout=self._config.rpc_timeout_s, owner_id=session.owner_id,
            )
        except ConnectorError:
            pass  # device may already be gone; the session is dropped regardless
        audit_desktop(
            self._workspace, session_id=session.session_id, node_id=session.node_id,
            operator_id=session.operator_id, action_type="session.end",
            params_summary={"reason": reason}, sensitive=False, confirmed=True, result="ok",
        )

    async def _confirm_sensitive(self, session: DesktopSession, atype: str, summary: dict) -> bool:
        if self._broker is None:
            return False  # fail-closed: no way to confirm ⇒ block
        pending = await self._broker.create(
            approval_id=uuid.uuid4().hex, node_id=session.node_id, tool=f"desktop:{atype}",
            operator_id=session.operator_id, args_summary=summary,
        )
        return await self._broker.wait(pending, ttl_s=self._config.approval_ttl_s)


# --- process-global singleton (mirrors the exec coordinator) -------------

_DEFAULT_DESKTOP_MANAGER: DesktopSessionManager | None = None


def set_default_desktop_manager(manager: DesktopSessionManager) -> None:
    global _DEFAULT_DESKTOP_MANAGER
    _DEFAULT_DESKTOP_MANAGER = manager


def default_desktop_manager() -> DesktopSessionManager | None:
    return _DEFAULT_DESKTOP_MANAGER
