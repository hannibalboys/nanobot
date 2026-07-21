"""Tests for ConnectorGateway v2 exec-management HTTP routes."""

from __future__ import annotations

import asyncio
import json

from nanobot.channels.websocket.runtime import WebSocketConfig
from nanobot.config.schema import ConnectorConfig
from nanobot.connector.gateway import ConnectorGateway
from nanobot.connector.hub import ConnectorHub

from .conftest import duplex
from .test_exec_integration import FakeExecConnector


class FakeHeaders(dict):
    def get(self, key, default=None):
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return default


class FakeRequest:
    def __init__(self, path: str, headers: dict | None = None):
        self.path = path
        self.headers = FakeHeaders(headers or {})


class FakeConnection:
    def __init__(self, remote=("1.2.3.4", 5555)):
        self.remote_address = remote


def _auth():
    return {"Authorization": "Bearer s3cret"}


def _body(resp):
    return json.loads(resp.body.decode("utf-8"))


def _gateway(tmp_path, *, allow_exec=True, allow_mcp_proxy=False, allow_desktop_control=False):
    cfg = ConnectorConfig(
        enabled=True, allow_exec=allow_exec, allow_mcp_proxy=allow_mcp_proxy,
        allow_desktop_control=allow_desktop_control,
    )
    ws = WebSocketConfig(token="s3cret")
    return ConnectorGateway(cfg, workspace_path=tmp_path, ws_config=ws, hub=ConnectorHub())


def _pair(gw, *, fingerprint="fp1"):
    code, _ = gw.devices.generate_pairing_code("webui")
    node_id, _tok = gw.devices.redeem_pairing_code(
        code, name="PC", platform="windows", machine_fingerprint=fingerprint
    )
    return node_id


async def _bring_online(gw, node_id, tools):
    server_conn, client_conn = duplex()
    serve_task = asyncio.create_task(gw.hub.serve(server_conn, node_id=node_id, owner_id="webui"))
    connector = FakeExecConnector(client_conn, tools=tools)
    ctask = asyncio.create_task(connector.run())
    for _ in range(200):
        if gw.hub.list_nodes():
            break
        await asyncio.sleep(0.01)
    return server_conn, serve_task, ctask


async def _get(gw, path):
    return await gw.handle_http(FakeConnection(), FakeRequest(path, _auth()), path.split("?")[0])


def test_exec_routes_hidden_when_allow_exec_off(tmp_path):
    gw = _gateway(tmp_path, allow_exec=False)
    assert not gw.owns_http_route("/api/connector/tools")
    assert not gw.owns_http_route("/api/connector/grant")
    # base routes still owned
    assert gw.owns_http_route("/api/connector/nodes")
    assert gw.owns_http_route("/api/connector/alias")


def test_exec_routes_present_when_allow_exec_on(tmp_path):
    gw = _gateway(tmp_path, allow_exec=True)
    assert gw.owns_http_route("/api/connector/tools")
    assert gw.owns_http_route("/api/connector/grant")
    assert gw.owns_http_route("/api/connector/exec-metrics")


async def test_alias_route(tmp_path):
    gw = _gateway(tmp_path)
    node_id = _pair(gw)
    resp = await _get(gw, f"/api/connector/alias?nodeId={node_id}&alias=Zhang's Laptop")
    assert resp.status_code == 200
    assert gw.devices.get(node_id).alias == "Zhang's Laptop"


async def test_alias_unknown_device_404(tmp_path):
    gw = _gateway(tmp_path)
    resp = await _get(gw, "/api/connector/alias?nodeId=ghost&alias=x")
    assert resp.status_code == 404


async def test_list_tools_route(tmp_path):
    gw = _gateway(tmp_path)
    node_id = _pair(gw)
    sconn, serve_task, ctask = await _bring_online(
        gw, node_id, [{"name": "printer", "approval": "auto", "params": []}]
    )
    resp = await _get(gw, f"/api/connector/tools?nodeId={node_id}")
    assert resp.status_code == 200
    assert [t["name"] for t in _body(resp)["tools"]] == ["printer"]
    await sconn.close()
    await asyncio.gather(serve_task, ctask)


async def test_grant_and_list_and_revoke(tmp_path):
    gw = _gateway(tmp_path)
    node_id = _pair(gw)

    granted = await _get(
        gw, f"/api/connector/grant?nodeId={node_id}&tool=printer&operatorId=alice&ttlS=3600"
    )
    assert granted.status_code == 200
    assert gw.authz.is_authorized(node_id, "printer", operator_id="alice", owner_id="webui")

    listed = await _get(gw, f"/api/connector/grants?nodeId={node_id}")
    assert listed.status_code == 200
    body = _body(listed)
    assert body["grants"][0]["operatorId"] == "alice"
    assert body["activeOperators"][0]["operatorId"] == "alice"

    revoked = await _get(
        gw, f"/api/connector/revoke-grant?nodeId={node_id}&tool=printer&operatorId=alice"
    )
    assert revoked.status_code == 200
    assert not gw.authz.is_authorized(node_id, "printer", operator_id="alice", owner_id="webui")


async def test_grant_unknown_device_404(tmp_path):
    gw = _gateway(tmp_path)
    resp = await _get(gw, "/api/connector/grant?nodeId=ghost&tool=t&operatorId=alice")
    assert resp.status_code == 404


async def test_requests_listing_scoped_to_owned(tmp_path):
    gw = _gateway(tmp_path)
    node_id = _pair(gw)
    gw.authz.request_access(node_id, "alice", tools=["printer"], reason="need")
    gw.authz.request_access("other-dev", "bob", tools=["x"])  # not owned/known
    resp = await _get(gw, "/api/connector/requests")
    reqs = _body(resp)["requests"]
    assert len(reqs) == 1
    assert reqs[0]["operatorId"] == "alice"


async def test_deny_request_route(tmp_path):
    gw = _gateway(tmp_path)
    node_id = _pair(gw)
    gw.authz.request_access(node_id, "alice", tools=["printer"])
    resp = await _get(gw, f"/api/connector/deny-request?nodeId={node_id}&operatorId=alice")
    assert resp.status_code == 200
    assert gw.authz.list_requests(node_id=node_id) == []
    # denying again → 404 (already gone)
    again = await _get(gw, f"/api/connector/deny-request?nodeId={node_id}&operatorId=alice")
    assert again.status_code == 404


async def test_deny_request_unknown_device_404(tmp_path):
    gw = _gateway(tmp_path)
    resp = await _get(gw, "/api/connector/deny-request?nodeId=ghost&operatorId=alice")
    assert resp.status_code == 404


# --- v2.5 MCP proxy route ------------------------------------------------


def test_mcp_route_gated_on_allow_mcp_proxy(tmp_path):
    off = _gateway(tmp_path, allow_exec=True, allow_mcp_proxy=False)
    assert not off.owns_http_route("/api/connector/mcp-tools")
    on = _gateway(tmp_path, allow_exec=True, allow_mcp_proxy=True)
    assert on.owns_http_route("/api/connector/mcp-tools")


async def test_mcp_tools_route(tmp_path):
    from .test_mcp_proxy import FakeMcpConnector

    gw = _gateway(tmp_path, allow_mcp_proxy=True)
    node_id = _pair(gw)
    server_conn, client_conn = duplex()
    serve_task = asyncio.create_task(gw.hub.serve(server_conn, node_id=node_id, owner_id="webui"))
    connector = FakeMcpConnector(
        client_conn,
        tools=[{"server": "fs", "name": "search", "inputSchema": {}, "approval": "auto"}],
    )
    ctask = asyncio.create_task(connector.run())
    for _ in range(200):
        if gw.hub.list_nodes():
            break
        await asyncio.sleep(0.01)

    resp = await _get(gw, f"/api/connector/mcp-tools?nodeId={node_id}")
    assert resp.status_code == 200
    body = _body(resp)
    assert [t["name"] for t in body["tools"]] == ["search"]
    assert body["servers"][0]["server"] == "fs"

    await server_conn.close()
    await asyncio.gather(serve_task, ctask)


# --- v3 desktop routes ---------------------------------------------------


def test_desktop_routes_gated_on_allow_desktop_control(tmp_path):
    off = _gateway(tmp_path, allow_desktop_control=False)
    assert not off.owns_http_route("/api/connector/desktop-sessions")
    assert not off.owns_http_route("/api/connector/desktop-takeover")
    on = _gateway(tmp_path, allow_desktop_control=True)
    assert on.owns_http_route("/api/connector/desktop-sessions")
    assert on.owns_http_route("/api/connector/desktop-takeover")


async def test_desktop_sessions_route_empty(tmp_path):
    gw = _gateway(tmp_path, allow_desktop_control=True)
    resp = await _get(gw, "/api/connector/desktop-sessions")
    assert resp.status_code == 200
    assert _body(resp)["sessions"] == []


async def test_desktop_takeover_unknown_session_404(tmp_path):
    gw = _gateway(tmp_path, allow_desktop_control=True)
    resp = await _get(gw, "/api/connector/desktop-takeover?sessionId=nope")
    assert resp.status_code == 404


def test_desktop_review_routes_gated(tmp_path):
    off = _gateway(tmp_path, allow_desktop_control=False)
    for r in ("/api/connector/desktop-audit", "/api/connector/desktop-recordings",
              "/api/connector/desktop-recording-delete"):
        assert not off.owns_http_route(r)
    on = _gateway(tmp_path, allow_desktop_control=True)
    for r in ("/api/connector/desktop-audit", "/api/connector/desktop-recordings",
              "/api/connector/desktop-recording-delete"):
        assert on.owns_http_route(r)


async def test_desktop_audit_and_recordings_routes(tmp_path):
    from nanobot.connector.desktop import audit_desktop

    gw = _gateway(tmp_path, allow_desktop_control=True)
    node_id = _pair(gw)
    audit_desktop(tmp_path, session_id="sess-1", node_id=node_id, operator_id="webui",
                  action_type="click", params_summary={}, sensitive=False, confirmed=True, result="ok")
    audit_desktop(tmp_path, session_id="sess-x", node_id="other-dev", operator_id="webui",
                  action_type="click", params_summary={}, sensitive=False, confirmed=True, result="ok")

    audit = _body(await _get(gw, "/api/connector/desktop-audit"))["records"]
    assert len(audit) == 1 and audit[0]["nodeId"] == node_id  # scoped to owned device

    # a recording for the owned device
    rec = tmp_path / "connector" / "desktop-recordings" / "sess-1"
    rec.mkdir(parents=True)
    (rec / "000001.png").write_bytes(b"x")
    recordings = _body(await _get(gw, "/api/connector/desktop-recordings"))["recordings"]
    assert any(r["sessionId"] == "sess-1" for r in recordings)

    deleted = await _get(gw, "/api/connector/desktop-recording-delete?sessionId=sess-1")
    assert deleted.status_code == 200
    assert not rec.exists()


async def test_desktop_recording_delete_unowned_404(tmp_path):
    gw = _gateway(tmp_path, allow_desktop_control=True)
    resp = await _get(gw, "/api/connector/desktop-recording-delete?sessionId=ghost")
    assert resp.status_code == 404


async def test_metrics_route(tmp_path):
    gw = _gateway(tmp_path)
    resp = await _get(gw, "/api/connector/exec-metrics")
    assert resp.status_code == 200
    snap = _body(resp)
    assert "executions" in snap and "failureRate" in snap


async def test_exec_audit_route_scoped_to_owned(tmp_path):
    from nanobot.connector.exec import audit_exec

    gw = _gateway(tmp_path)
    node_id = _pair(gw)
    # one record for an owned device, one for an unknown device
    audit_exec(tmp_path, operator_id="webui", node_id=node_id, tool="printer",
               args_summary={}, approval="auto", result="ok", exit_code=0)
    audit_exec(tmp_path, operator_id="webui", node_id="other-dev", tool="x",
               args_summary={}, approval="auto", result="ok")
    resp = await _get(gw, "/api/connector/exec-audit")
    records = _body(resp)["records"]
    assert len(records) == 1
    assert records[0]["nodeId"] == node_id


async def test_exec_audit_route_hidden_without_allow_exec(tmp_path):
    gw = _gateway(tmp_path, allow_exec=False)
    assert not gw.owns_http_route("/api/connector/exec-audit")


async def test_approve_not_found(tmp_path):
    gw = _gateway(tmp_path)
    resp = await _get(gw, "/api/connector/approve?approvalId=nope&decision=approve")
    assert resp.status_code == 404


async def test_webui_self_approval_loop_through_routes(tmp_path):
    """Fix A + E: the device owner can approve their own webui-policy execution
    through the HTTP routes (the old broker self-check wrongly blocked this)."""
    gw = _gateway(tmp_path)
    node_id = _pair(gw)
    sconn, serve_task, ctask = await _bring_online(
        gw, node_id, [{"name": "printer", "approval": "webui", "params": []}]
    )

    call = asyncio.create_task(
        gw.coordinator.call_tool(node_id, "printer", {}, operator_id="webui", owner_id="webui")
    )

    # the pending approval shows up on the approvals route
    approval_id = None
    for _ in range(100):
        resp = await _get(gw, "/api/connector/approvals")
        pending = _body(resp)["approvals"]
        if pending:
            approval_id = pending[0]["approvalId"]
            break
        await asyncio.sleep(0.02)
    assert approval_id is not None

    approve = await _get(gw, f"/api/connector/approve?approvalId={approval_id}&decision=approve")
    assert approve.status_code == 200
    assert _body(approve)["approved"] is True

    result = await call
    assert result.exit_code == 0

    await sconn.close()
    await asyncio.gather(serve_task, ctask)
