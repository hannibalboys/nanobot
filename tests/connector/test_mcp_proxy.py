"""Integration tests for the local MCP proxy: hub routing + coordinator (v2.5)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from nanobot.connector import protocol as proto
from nanobot.connector.authz import AuthorizationStore
from nanobot.connector.exec import (
    ApprovalBroker,
    ExecMetrics,
    ExecutionCoordinator,
    RateLimiter,
    mcp_tool_key,
)
from nanobot.connector.hub import ConnectorError, ConnectorHub

from .conftest import FakeConn, duplex


class FakeMcpConnector:
    """A connector that bridges MCP servers: answers mcp.list / mcp.call."""

    def __init__(
        self,
        conn: FakeConn,
        *,
        tools: list[dict],
        servers: list[dict] | None = None,
        call_result: dict | None = None,
        capabilities: list[str] | None = None,
    ) -> None:
        self.conn = conn
        self.tools = tools
        self.servers = servers if servers is not None else [{"server": "fs", "healthy": True, "toolCount": len(tools)}]
        self.call_result = call_result if call_result is not None else {"content": "ok", "isError": False}
        self.capabilities = capabilities if capabilities is not None else [proto.CAP_FS, proto.CAP_EXEC, proto.CAP_MCP]
        self.calls: list[tuple[str, str, dict]] = []

    async def run(self):
        await self.conn.send(json.dumps(proto.dump_frame(proto.RegisterFrame(
            node=proto.NodeInfo(name="fake-mcp", platform="test", capabilities=self.capabilities),
        ))))
        async for raw in self.conn:
            frame = proto.parse_frame(json.loads(raw))
            if isinstance(frame, proto.RegisteredFrame):
                continue
            if isinstance(frame, proto.RpcRequestFrame):
                await self._handle(frame)

    async def _handle(self, frame: proto.RpcRequestFrame):
        if frame.method == "mcp.list":
            await self._respond(frame.id, {"tools": self.tools, "servers": self.servers})
        elif frame.method == "mcp.call":
            self.calls.append((frame.params.get("server"), frame.params.get("tool"), frame.params.get("args")))
            await self._respond(frame.id, self.call_result)
        else:
            await self._send(proto.RpcResponseFrame(id=frame.id, ok=False, error={"code": proto.ERROR_INTERNAL, "message": "unknown"}))

    async def _respond(self, rpc_id, result):
        await self._send(proto.RpcResponseFrame(id=rpc_id, ok=True, result=result))

    async def _send(self, frame):
        await self.conn.send(json.dumps(proto.dump_frame(frame)))


async def _online(**kwargs):
    server_conn, client_conn = duplex()
    hub = ConnectorHub()
    connector = FakeMcpConnector(client_conn, **kwargs)
    serve_task = asyncio.create_task(hub.serve(server_conn, node_id="dev-1", owner_id="webui"))
    ctask = asyncio.create_task(connector.run())
    for _ in range(200):
        if hub.list_nodes():
            break
        await asyncio.sleep(0.01)
    return hub, serve_task, ctask, connector, server_conn


_TOOL = {"server": "fs", "name": "search", "description": "find", "inputSchema": {}, "approval": "auto"}


def _cfg(**over):
    base = dict(
        rpc_timeout_s=2, exec_timeout_s=3, max_exec_output_bytes=10_000,
        max_concurrent_execs=2, approval_ttl_s=1, exec_rate_per_minute=100,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _coord(hub, tmp_path, **over):
    cfg = _cfg(**over)
    return ExecutionCoordinator(
        hub=hub, authz=AuthorizationStore(tmp_path / "grants.json"), workspace=tmp_path,
        config=cfg, metrics=ExecMetrics(), broker=ApprovalBroker(),
        rate_limiter=RateLimiter(per_minute=cfg.exec_rate_per_minute),
    )


# --- hub-level -----------------------------------------------------------


async def test_hub_list_mcp_tools():
    hub, serve_task, ctask, _c, sconn = await _online(tools=[_TOOL])
    tools = await hub.list_mcp_tools("dev-1", timeout=2)
    assert tools[0]["name"] == "search"
    await sconn.close()
    await asyncio.gather(serve_task, ctask)


async def test_hub_call_mcp_tool():
    hub, serve_task, ctask, connector, sconn = await _online(
        tools=[_TOOL], call_result={"content": "found", "isError": False}
    )
    result = await hub.call_mcp_tool("dev-1", "fs", "search", {"q": "x"}, timeout=3)
    assert result["content"] == "found"
    assert connector.calls == [("fs", "search", {"q": "x"})]
    await sconn.close()
    await asyncio.gather(serve_task, ctask)


async def test_hub_mcp_unsupported_device():
    hub, serve_task, ctask, _c, sconn = await _online(tools=[], capabilities=[proto.CAP_FS, proto.CAP_EXEC])
    with pytest.raises(ConnectorError) as ei:
        await hub.list_mcp_tools("dev-1", timeout=2)
    assert ei.value.code == proto.ERROR_MCP_UNSUPPORTED
    await sconn.close()
    await asyncio.gather(serve_task, ctask)


async def test_hub_mcp_status():
    hub, serve_task, ctask, _c, sconn = await _online(tools=[_TOOL])
    status = await hub.mcp_status("dev-1", timeout=2)
    assert status["tools"][0]["name"] == "search"
    assert status["servers"][0]["server"] == "fs"
    await sconn.close()
    await asyncio.gather(serve_task, ctask)


# --- coordinator-level ---------------------------------------------------


async def test_coordinator_mcp_self_use(tmp_path):
    hub, serve_task, ctask, _c, sconn = await _online(tools=[_TOOL], call_result={"content": "hi", "isError": False})
    coord = _coord(hub, tmp_path)
    result = await coord.call_mcp_tool("dev-1", "fs", "search", {}, operator_id="webui", owner_id="webui")
    assert result["content"] == "hi"
    assert coord._metrics.snapshot()["executions"] == 1
    await sconn.close()
    await asyncio.gather(serve_task, ctask)


async def test_coordinator_mcp_cross_person_hidden(tmp_path):
    hub, serve_task, ctask, _c, sconn = await _online(tools=[_TOOL])
    coord = _coord(hub, tmp_path)
    with pytest.raises(ConnectorError) as ei:
        await coord.call_mcp_tool("dev-1", "fs", "search", {}, operator_id="alice", owner_id="webui")
    assert ei.value.code == proto.ERROR_TOOL_NOT_FOUND
    # list is filtered too
    assert await coord.list_mcp_tools("dev-1", operator_id="alice", owner_id="webui") == []
    await sconn.close()
    await asyncio.gather(serve_task, ctask)


async def test_coordinator_mcp_cross_person_after_grant(tmp_path):
    hub, serve_task, ctask, _c, sconn = await _online(tools=[_TOOL], call_result={"content": "ok", "isError": False})
    coord = _coord(hub, tmp_path)
    coord._authz.grant("dev-1", mcp_tool_key("fs", "search"), "alice", granted_by="webui")
    result = await coord.call_mcp_tool("dev-1", "fs", "search", {}, operator_id="alice", owner_id="webui")
    assert result["content"] == "ok"
    tools = await coord.list_mcp_tools("dev-1", operator_id="alice", owner_id="webui")
    assert [t["name"] for t in tools] == ["search"]
    await sconn.close()
    await asyncio.gather(serve_task, ctask)


async def test_coordinator_mcp_webui_approval_default_deny(tmp_path):
    hub, serve_task, ctask, _c, sconn = await _online(
        tools=[{"server": "fs", "name": "search", "inputSchema": {}, "approval": "webui"}]
    )
    coord = _coord(hub, tmp_path, approval_ttl_s=1)
    with pytest.raises(ConnectorError) as ei:
        await coord.call_mcp_tool("dev-1", "fs", "search", {}, operator_id="webui", owner_id="webui")
    assert ei.value.code == proto.ERROR_APPROVAL_DENIED
    await sconn.close()
    await asyncio.gather(serve_task, ctask)


async def test_coordinator_mcp_unknown_tool(tmp_path):
    hub, serve_task, ctask, _c, sconn = await _online(tools=[_TOOL])
    coord = _coord(hub, tmp_path)
    with pytest.raises(ConnectorError) as ei:
        await coord.call_mcp_tool("dev-1", "fs", "ghost", {}, operator_id="webui", owner_id="webui")
    assert ei.value.code == proto.ERROR_TOOL_NOT_FOUND
    await sconn.close()
    await asyncio.gather(serve_task, ctask)
