"""End-to-end desktop control: full stack through gateway + manager + agent tools.

Single-owner self-use chain: authorize (device consent) → capture → act → sensitive
confirm → take over over HTTP → audit written; frames not persisted by default.
"""

from __future__ import annotations

import asyncio
import json

from nanobot.agent.tools.connector import (
    ConnectorDesktopActTool,
    ConnectorDesktopSessionTool,
)
from nanobot.config.schema import ConnectorConfig

from .conftest import duplex
from .test_desktop import FakeDesktopConnector
from .test_gateway_exec_http import FakeConnection, FakeRequest, _auth, _body, _gateway


async def _online(gw, *, approve_session=True, fingerprint="fp-desk-e2e"):
    code, _ = gw.devices.generate_pairing_code("webui")
    node_id, _tok = gw.devices.redeem_pairing_code(
        code, name="PC", platform="windows", machine_fingerprint=fingerprint
    )
    server_conn, client_conn = duplex()
    serve_task = asyncio.create_task(gw.hub.serve(server_conn, node_id=node_id, owner_id="webui"))
    connector = FakeDesktopConnector(client_conn, approve_session=approve_session)
    ctask = asyncio.create_task(connector.run())
    for _ in range(200):
        if gw.hub.list_nodes():
            break
        await asyncio.sleep(0.01)
    return node_id, server_conn, serve_task, ctask, connector


async def _get(gw, path):
    return await gw.handle_http(FakeConnection(), FakeRequest(path, _auth()), path.split("?")[0])


def _tools(gw, tmp_path):
    cfg = ConnectorConfig(enabled=True, allow_desktop_control=True)
    session_tool = ConnectorDesktopSessionTool(connector_config=cfg, workspace=tmp_path, manager=gw.desktop)
    act_tool = ConnectorDesktopActTool(connector_config=cfg, workspace=tmp_path, manager=gw.desktop)
    return session_tool, act_tool


async def test_e2e_desktop_session_and_act(tmp_path):
    gw = _gateway(tmp_path, allow_desktop_control=True)
    node_id, sconn, serve_task, ctask, connector = await _online(gw)
    session_tool, act_tool = _tools(gw, tmp_path)

    # agent opens a session (device consents) → gets first screenshot
    started = json.loads(await session_tool.execute(node_id=node_id, goal="open the app"))
    sid = started["sessionId"]
    assert started["image"] == "aW1n" and started["width"] == 800

    # agent acts on the screenshot
    out = json.loads(await act_tool.execute(session_id=sid, action={"type": "click", "x": 10, "y": 20}))
    assert out["image"] == "aW1n"
    assert connector.injected == [{"type": "click", "x": 10, "y": 20}]

    # session visible over the management route; owner takes over
    listed = _body(await _get(gw, "/api/connector/desktop-sessions"))["sessions"]
    assert any(s["sessionId"] == sid for s in listed)
    taken = await _get(gw, f"/api/connector/desktop-takeover?sessionId={sid}")
    assert _body(taken)["takenOver"] == sid

    # after take over, further acts fail
    err = await act_tool.execute(session_id=sid, action={"type": "click", "x": 1, "y": 1})
    assert err.is_error

    # per-action audit was written; no frames persisted (recording off)
    audit = (tmp_path / "connector" / "desktop-audit.log").read_text(encoding="utf-8")
    assert '"action": "click"' in audit
    rec = tmp_path / "connector" / "desktop-recordings"
    assert not rec.exists() or not any(rec.rglob("*"))

    await sconn.close()
    await asyncio.gather(serve_task, ctask)


async def test_e2e_device_denies_consent(tmp_path):
    gw = _gateway(tmp_path, allow_desktop_control=True)
    node_id, sconn, serve_task, ctask, connector = await _online(gw, approve_session=False)
    session_tool, _act = _tools(gw, tmp_path)
    out = await session_tool.execute(node_id=node_id, goal="x")
    assert out.is_error  # device owner did not consent
    await sconn.close()
    await asyncio.gather(serve_task, ctask)


async def test_e2e_sensitive_action_confirm_loop(tmp_path):
    gw = _gateway(tmp_path, allow_desktop_control=True)
    node_id, sconn, serve_task, ctask, connector = await _online(gw)
    session_tool, act_tool = _tools(gw, tmp_path)
    started = json.loads(await session_tool.execute(node_id=node_id, goal="pay"))
    sid = started["sessionId"]

    async def approve_soon():
        for _ in range(200):
            pending = _body(await _get(gw, "/api/connector/approvals"))["approvals"]
            if pending:
                await _get(gw, f"/api/connector/approve?approvalId={pending[0]['approvalId']}&decision=approve")
                return
            await asyncio.sleep(0.02)

    tk = asyncio.create_task(approve_soon())
    out = json.loads(await act_tool.execute(
        session_id=sid, action={"type": "click", "label": "Confirm payment", "x": 5, "y": 5}
    ))
    await tk
    assert out["image"] == "aW1n"  # injected after confirmation
    assert connector.injected  # the sensitive click did run

    await sconn.close()
    await asyncio.gather(serve_task, ctask)
