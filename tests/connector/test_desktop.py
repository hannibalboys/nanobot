"""Integration tests for server-side desktop control (add-connector-desktop-control)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from nanobot.connector import protocol as proto
from nanobot.connector.authz import AuthorizationStore
from nanobot.connector.desktop import (
    ApprovalBroker,
    DesktopSessionManager,
    cleanup_recordings,
    is_sensitive_action,
)
from nanobot.connector.hub import ConnectorError, ConnectorHub

from .conftest import FakeConn, duplex


class FakeDesktopConnector:
    """A device that supports desktop control: answers desktop.* methods."""

    def __init__(
        self,
        conn: FakeConn,
        *,
        approve_session: bool = True,
        width: int = 800,
        height: int = 600,
        capabilities: list[str] | None = None,
    ) -> None:
        self.conn = conn
        self.approve_session = approve_session
        self.width = width
        self.height = height
        self.capabilities = capabilities if capabilities is not None else [proto.CAP_FS, proto.CAP_DESKTOP]
        self.injected: list[dict] = []
        self.active = False

    async def run(self):
        await self.conn.send(json.dumps(proto.dump_frame(proto.RegisterFrame(
            node=proto.NodeInfo(name="fake-desktop", platform="test", capabilities=self.capabilities),
        ))))
        async for raw in self.conn:
            frame = proto.parse_frame(json.loads(raw))
            if isinstance(frame, proto.RegisteredFrame):
                continue
            if isinstance(frame, proto.RpcRequestFrame):
                await self._handle(frame)

    async def _handle(self, frame: proto.RpcRequestFrame):
        m = frame.method
        if m == "desktop.session.start":
            if not self.approve_session:
                await self._err(frame.id, proto.ERROR_SESSION_ENDED, "denied on device")
                return
            self.active = True
            await self._ok(frame.id, {"started": frame.params.get("sessionId")})
        elif m == "desktop.capture":
            await self._ok(frame.id, {"image": "aW1n", "width": self.width, "height": self.height, "format": "png"})
        elif m == "desktop.input":
            self.injected.append(frame.params.get("action"))
            await self._ok(frame.id, {"ok": True})
        elif m == "desktop.session.end":
            self.active = False
            await self._ok(frame.id, {"ended": True})
        else:
            await self._err(frame.id, proto.ERROR_INTERNAL, "unknown")

    async def _ok(self, rpc_id, result):
        await self.conn.send(json.dumps(proto.dump_frame(proto.RpcResponseFrame(id=rpc_id, ok=True, result=result))))

    async def _err(self, rpc_id, code, msg):
        await self.conn.send(json.dumps(proto.dump_frame(
            proto.RpcResponseFrame(id=rpc_id, ok=False, error={"code": code, "message": msg})
        )))


async def _online(**kwargs):
    server_conn, client_conn = duplex()
    hub = ConnectorHub()
    connector = FakeDesktopConnector(client_conn, **kwargs)
    serve_task = asyncio.create_task(hub.serve(server_conn, node_id="dev-1", owner_id="webui"))
    ctask = asyncio.create_task(connector.run())
    for _ in range(200):
        if hub.list_nodes():
            break
        await asyncio.sleep(0.01)
    return hub, serve_task, ctask, connector, server_conn


def _cfg(**over):
    base = dict(
        rpc_timeout_s=2, approval_ttl_s=1,
        desktop_session_max_s=900, desktop_idle_timeout_s=120,
        desktop_recording_retention_days=7,
        desktop_max_dimension=1280, desktop_max_fps=2,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _manager(hub, tmp_path, *, broker=None, **cfg_over):
    return DesktopSessionManager(
        hub=hub, authz=AuthorizationStore(tmp_path / "grants.json"),
        workspace=tmp_path, config=_cfg(**cfg_over), broker=broker or ApprovalBroker(),
    )


# --- sensitive detection (unit) ------------------------------------------


def test_is_sensitive_action():
    assert is_sensitive_action({"type": "click", "label": "Confirm payment"})
    assert is_sensitive_action({"type": "click", "label": "删除账户"})
    assert is_sensitive_action({"type": "type", "text": "x", "secret": True})
    assert is_sensitive_action({"type": "click", "sensitive": True})
    assert not is_sensitive_action({"type": "click", "label": "Cancel"})
    assert not is_sensitive_action({"type": "type", "text": "hello"})


# --- manager lifecycle ---------------------------------------------------


async def test_start_capture_act_end(tmp_path):
    hub, serve_task, ctask, connector, sconn = await _online()
    mgr = _manager(hub, tmp_path)
    started = await mgr.start("dev-1", operator_id="webui", owner_id="webui", goal="open app")
    assert started["width"] == 800 and started["image"] == "aW1n"
    sid = started["sessionId"]

    frame = await mgr.capture(sid, operator_id="webui")
    assert frame["height"] == 600

    nxt = await mgr.act(sid, {"type": "click", "x": 10, "y": 20}, operator_id="webui")
    assert connector.injected == [{"type": "click", "x": 10, "y": 20}]
    assert nxt["image"] == "aW1n"  # act returns next frame

    await mgr.end(sid, operator_id="webui")
    with pytest.raises(ConnectorError) as ei:
        await mgr.capture(sid, operator_id="webui")
    assert ei.value.code == proto.ERROR_SESSION_ENDED
    await sconn.close()
    await asyncio.gather(serve_task, ctask)


async def test_device_denies_session(tmp_path):
    hub, serve_task, ctask, connector, sconn = await _online(approve_session=False)
    mgr = _manager(hub, tmp_path)
    with pytest.raises(ConnectorError) as ei:
        await mgr.start("dev-1", operator_id="webui", owner_id="webui", goal="x")
    assert ei.value.code == proto.ERROR_SESSION_ENDED
    await sconn.close()
    await asyncio.gather(serve_task, ctask)


async def test_unsupported_device(tmp_path):
    hub, serve_task, ctask, connector, sconn = await _online(capabilities=[proto.CAP_FS])
    mgr = _manager(hub, tmp_path)
    with pytest.raises(ConnectorError) as ei:
        await mgr.start("dev-1", operator_id="webui", owner_id="webui", goal="x")
    assert ei.value.code == proto.ERROR_DESKTOP_UNSUPPORTED
    await sconn.close()
    await asyncio.gather(serve_task, ctask)


async def test_sensitive_action_blocked_without_confirm(tmp_path):
    hub, serve_task, ctask, connector, sconn = await _online()
    mgr = _manager(hub, tmp_path, approval_ttl_s=1)  # broker present, nobody approves
    started = await mgr.start("dev-1", operator_id="webui", owner_id="webui", goal="x")
    with pytest.raises(ConnectorError) as ei:
        await mgr.act(started["sessionId"], {"type": "click", "label": "Confirm payment", "x": 1, "y": 1},
                      operator_id="webui")
    assert ei.value.code == proto.ERROR_SENSITIVE_UNCONFIRMED
    assert connector.injected == []  # never injected
    await sconn.close()
    await asyncio.gather(serve_task, ctask)


async def test_sensitive_action_injected_after_confirm(tmp_path):
    hub, serve_task, ctask, connector, sconn = await _online()
    broker = ApprovalBroker()
    mgr = _manager(hub, tmp_path, broker=broker, approval_ttl_s=5)
    started = await mgr.start("dev-1", operator_id="webui", owner_id="webui", goal="x")

    async def approve_soon():
        for _ in range(100):
            pending = broker.list_pending()
            if pending:
                await broker.resolve(pending[0]["approvalId"], approved=True)
                return
            await asyncio.sleep(0.02)

    tk = asyncio.create_task(approve_soon())
    await mgr.act(started["sessionId"], {"type": "click", "label": "Confirm", "x": 1, "y": 1}, operator_id="webui")
    await tk
    assert len(connector.injected) == 1
    await sconn.close()
    await asyncio.gather(serve_task, ctask)


async def test_idle_timeout_ends_session(tmp_path):
    hub, serve_task, ctask, connector, sconn = await _online()
    mgr = _manager(hub, tmp_path, desktop_idle_timeout_s=60)
    started = await mgr.start("dev-1", operator_id="webui", owner_id="webui", goal="x")
    # Deterministically age the session past the idle window (no real-time flakiness).
    mgr._sessions[started["sessionId"]].last_activity -= 10_000
    with pytest.raises(ConnectorError) as ei:
        await mgr.act(started["sessionId"], {"type": "click", "x": 1, "y": 1}, operator_id="webui")
    assert ei.value.code == proto.ERROR_SESSION_ENDED
    await sconn.close()
    await asyncio.gather(serve_task, ctask)


async def test_take_over_ends_session(tmp_path):
    hub, serve_task, ctask, connector, sconn = await _online()
    mgr = _manager(hub, tmp_path)
    started = await mgr.start("dev-1", operator_id="webui", owner_id="webui", goal="x")
    assert await mgr.take_over(started["sessionId"], owner_id="webui") is True
    with pytest.raises(ConnectorError):
        await mgr.capture(started["sessionId"], operator_id="webui")
    await sconn.close()
    await asyncio.gather(serve_task, ctask)


async def test_cross_person_requires_grant(tmp_path):
    hub, serve_task, ctask, connector, sconn = await _online()
    mgr = _manager(hub, tmp_path)
    with pytest.raises(ConnectorError) as ei:
        await mgr.start("dev-1", operator_id="alice", owner_id="webui", goal="x")
    assert ei.value.code == proto.ERROR_DESKTOP_UNSUPPORTED  # hidden
    # grant then allowed
    from nanobot.connector.desktop import DESKTOP_AUTHZ_KEY
    mgr._authz.grant("dev-1", DESKTOP_AUTHZ_KEY, "alice", granted_by="webui")
    started = await mgr.start("dev-1", operator_id="alice", owner_id="webui", goal="x")
    assert "sessionId" in started
    await sconn.close()
    await asyncio.gather(serve_task, ctask)


async def test_frames_not_persisted_by_default(tmp_path):
    hub, serve_task, ctask, connector, sconn = await _online()
    mgr = _manager(hub, tmp_path)
    started = await mgr.start("dev-1", operator_id="webui", owner_id="webui", goal="x")
    await mgr.capture(started["sessionId"], operator_id="webui")
    rec = tmp_path / "connector" / "desktop-recordings"
    assert not rec.exists() or not any(rec.rglob("*.png"))
    await sconn.close()
    await asyncio.gather(serve_task, ctask)


async def test_recording_persists_when_enabled(tmp_path):
    hub, serve_task, ctask, connector, sconn = await _online()
    mgr = _manager(hub, tmp_path)
    started = await mgr.start("dev-1", operator_id="webui", owner_id="webui", goal="x", record=True)
    await mgr.capture(started["sessionId"], operator_id="webui")
    rec = tmp_path / "connector" / "desktop-recordings" / started["sessionId"]
    assert any(rec.glob("*.png"))
    await sconn.close()
    await asyncio.gather(serve_task, ctask)


async def test_idle_expiry_tells_device_to_stop(tmp_path):
    """Fix A: an expired session must send desktop.session.end to the device,
    not just drop the server record (otherwise the device keeps capturing)."""
    hub, serve_task, ctask, connector, sconn = await _online()
    mgr = _manager(hub, tmp_path, desktop_idle_timeout_s=60)
    started = await mgr.start("dev-1", operator_id="webui", owner_id="webui", goal="x")
    assert connector.active is True
    mgr._sessions[started["sessionId"]].last_activity -= 10_000
    with pytest.raises(ConnectorError):
        await mgr.capture(started["sessionId"], operator_id="webui")
    # the device received session.end and stopped
    assert connector.active is False
    await sconn.close()
    await asyncio.gather(serve_task, ctask)


async def test_start_capture_failure_no_orphan_session(tmp_path):
    """Fix B: if the first capture fails, no session is left open."""
    hub, serve_task, ctask, connector, sconn = await _online()
    mgr = _manager(hub, tmp_path)

    # make capture fail once by pointing at a disconnected node mid-start is hard;
    # instead monkeypatch _capture to raise the first time.
    orig = mgr._capture
    calls = {"n": 0}

    async def flaky(session):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectorError(proto.ERROR_INTERNAL, "boom")
        return await orig(session)

    mgr._capture = flaky
    with pytest.raises(ConnectorError):
        await mgr.start("dev-1", operator_id="webui", owner_id="webui", goal="x")
    assert mgr.list_sessions(owner_id="webui") == []  # no orphan
    assert connector.active is False  # device told to end
    await sconn.close()
    await asyncio.gather(serve_task, ctask)


async def test_list_sessions_prunes_offline_devices(tmp_path):
    """Fix C: sessions for disconnected devices are pruned from the list."""
    hub, serve_task, ctask, connector, sconn = await _online()
    mgr = _manager(hub, tmp_path)
    await mgr.start("dev-1", operator_id="webui", owner_id="webui", goal="x")
    assert len(mgr.list_sessions(owner_id="webui")) == 1
    # device disconnects
    await sconn.close()
    await asyncio.gather(serve_task, ctask)
    assert mgr.list_sessions(owner_id="webui") == []  # phantom pruned
    assert mgr._sessions == {}


async def test_desktop_audit_masks_typed_text(tmp_path):
    """Fix E: the desktop audit must not persist what was typed."""
    hub, serve_task, ctask, connector, sconn = await _online()
    mgr = _manager(hub, tmp_path)
    started = await mgr.start("dev-1", operator_id="webui", owner_id="webui", goal="x")
    await mgr.act(started["sessionId"], {"type": "type", "text": "hunter2-secret"}, operator_id="webui")
    audit = (tmp_path / "connector" / "desktop-audit.log").read_text(encoding="utf-8")
    assert "hunter2-secret" not in audit
    assert "chars>" in audit  # masked as "<N chars>"
    await sconn.close()
    await asyncio.gather(serve_task, ctask)


def test_cleanup_recordings(tmp_path):
    import os
    import time as _t

    rec = tmp_path / "connector" / "desktop-recordings" / "old-session"
    rec.mkdir(parents=True)
    (rec / "000001.png").write_bytes(b"x")
    old = _t.time() - 40 * 86400
    os.utime(rec, (old, old))
    removed = cleanup_recordings(tmp_path, retention_days=7)
    assert removed == 1
    assert not rec.exists()


async def test_read_desktop_audit_replay(tmp_path):
    hub, serve_task, ctask, connector, sconn = await _online()
    mgr = _manager(hub, tmp_path)
    started = await mgr.start("dev-1", operator_id="webui", owner_id="webui", goal="x")
    await mgr.act(started["sessionId"], {"type": "click", "x": 1, "y": 2}, operator_id="webui")
    from nanobot.connector.desktop import read_desktop_audit

    records = read_desktop_audit(tmp_path, session_id=started["sessionId"])
    actions = [r["action"] for r in records]
    assert "click" in actions and "session.start" in actions
    # newest-first
    assert records[0]["action"] == "click"
    await sconn.close()
    await asyncio.gather(serve_task, ctask)


async def test_list_and_delete_recording(tmp_path):
    from nanobot.connector.desktop import delete_recording, list_recordings

    hub, serve_task, ctask, connector, sconn = await _online()
    mgr = _manager(hub, tmp_path)
    started = await mgr.start("dev-1", operator_id="webui", owner_id="webui", goal="x", record=True)
    await mgr.capture(started["sessionId"], operator_id="webui")
    recs = list_recordings(tmp_path)
    assert any(r["sessionId"] == started["sessionId"] and r["frames"] >= 1 for r in recs)

    assert delete_recording(tmp_path, started["sessionId"]) is True
    assert delete_recording(tmp_path, started["sessionId"]) is False  # already gone
    await sconn.close()
    await asyncio.gather(serve_task, ctask)


def test_delete_recording_rejects_traversal(tmp_path):
    from nanobot.connector.desktop import delete_recording

    assert delete_recording(tmp_path, "../../etc") is False
    assert delete_recording(tmp_path, "a/b") is False
    assert delete_recording(tmp_path, "") is False
