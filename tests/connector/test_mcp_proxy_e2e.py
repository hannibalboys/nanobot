"""End-to-end local MCP proxy: full stack through gateway routes + coordinator.

Covers the chain: bridge a device's MCP server → list over HTTP → cross-person
request/grant → agent call (auto / webui) → device offline deregisters.
"""

from __future__ import annotations

import asyncio

import pytest

from nanobot.connector import protocol as proto
from nanobot.connector.exec import mcp_tool_key
from nanobot.connector.hub import ConnectorError

from .conftest import duplex
from .test_gateway_exec_http import FakeConnection, FakeRequest, _auth, _body, _gateway
from .test_mcp_proxy import FakeMcpConnector


async def _online(gw, tools, *, call_result=None, fingerprint="fp-mcp-e2e"):
    code, _ = gw.devices.generate_pairing_code("webui")
    node_id, _tok = gw.devices.redeem_pairing_code(
        code, name="PC", platform="windows", machine_fingerprint=fingerprint
    )
    server_conn, client_conn = duplex()
    serve_task = asyncio.create_task(gw.hub.serve(server_conn, node_id=node_id, owner_id="webui"))
    connector = FakeMcpConnector(client_conn, tools=tools, call_result=call_result)
    ctask = asyncio.create_task(connector.run())
    for _ in range(200):
        if gw.hub.list_nodes():
            break
        await asyncio.sleep(0.01)
    return node_id, server_conn, serve_task, ctask


async def _get(gw, path):
    return await gw.handle_http(FakeConnection(), FakeRequest(path, _auth()), path.split("?")[0])


_TOOL = {"server": "fs", "name": "search", "description": "find", "inputSchema": {}, "approval": "auto"}


async def test_e2e_mcp_list_and_self_call(tmp_path):
    gw = _gateway(tmp_path, allow_mcp_proxy=True)
    node_id, sconn, serve_task, ctask = await _online(
        gw, [_TOOL], call_result={"content": "results", "isError": False}
    )

    # WebUI lists bridged tools + server health over HTTP
    listed = _body(await _get(gw, f"/api/connector/mcp-tools?nodeId={node_id}"))
    assert [t["name"] for t in listed["tools"]] == ["search"]
    assert listed["servers"][0]["healthy"] is True

    # agent calls the bridged tool via the coordinator
    result = await gw.coordinator.call_mcp_tool(
        node_id, "fs", "search", {"q": "x"}, operator_id="webui", owner_id="webui"
    )
    assert result["content"] == "results"

    await sconn.close()
    await asyncio.gather(serve_task, ctask)


async def test_e2e_mcp_cross_person_request_grant(tmp_path):
    gw = _gateway(tmp_path, allow_mcp_proxy=True)
    node_id, sconn, serve_task, ctask = await _online(gw, [_TOOL])

    # alice denied before grant (hidden as not-found)
    with pytest.raises(ConnectorError) as ei:
        await gw.coordinator.call_mcp_tool(
            node_id, "fs", "search", {}, operator_id="alice", owner_id="webui"
        )
    assert ei.value.code == proto.ERROR_TOOL_NOT_FOUND

    # owner grants the mcp tool key over HTTP
    key = mcp_tool_key("fs", "search")
    granted = await _get(
        gw, f"/api/connector/grant?nodeId={node_id}&tool={key}&operatorId=alice"
    )
    assert granted.status_code == 200

    result = await gw.coordinator.call_mcp_tool(
        node_id, "fs", "search", {}, operator_id="alice", owner_id="webui"
    )
    assert result["isError"] is False

    await sconn.close()
    await asyncio.gather(serve_task, ctask)


async def test_e2e_mcp_offline_deregisters(tmp_path):
    gw = _gateway(tmp_path, allow_mcp_proxy=True)
    node_id, sconn, serve_task, ctask = await _online(gw, [_TOOL])

    # online: tools listed
    assert _body(await _get(gw, f"/api/connector/mcp-tools?nodeId={node_id}"))["tools"]

    # device goes offline → route reports it as unavailable (409), tools gone
    await sconn.close()
    await asyncio.gather(serve_task, ctask)
    resp = await _get(gw, f"/api/connector/mcp-tools?nodeId={node_id}")
    assert resp.status_code == 409  # node offline / unsupported
