"""Client dispatch of mcp.list / mcp.call over the connector channel."""

from __future__ import annotations

import asyncio
import contextlib
import json

import pytest

from nanobot_connector.client import ConnectorClient
from nanobot_connector.config import ConnectorClientConfig
from nanobot_connector.mcp_bridge import McpBridge, McpRegistry, McpServerDef


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("NANOBOT_CONNECTOR_HOME", str(tmp_path / "home"))


class FakeTool:
    def __init__(self, name):
        self.name = name
        self.description = ""
        self.inputSchema = {}


class _Content:
    def __init__(self, text):
        self.text = text


class _CallResult:
    def __init__(self, text):
        self.content = [_Content(text)]
        self.isError = False


class FakeSession:
    def __init__(self, tools, call_text="ok"):
        self._tools = tools
        self._call_text = call_text

    async def initialize(self):
        return None

    async def list_tools(self):
        return type("L", (), {"tools": self._tools})()

    async def call_tool(self, name, arguments):
        return _CallResult(self._call_text)


class FakeWS:
    def __init__(self):
        self.frames = []

    async def send(self, data):
        self.frames.append(json.loads(data))


def _factory(session):
    @contextlib.asynccontextmanager
    async def factory(sdef, env):
        yield session

    return factory


async def _bridge_with(session):
    bridge = McpBridge(
        McpRegistry([McpServerDef(name="fs", command="x", approval="auto")]),
        session_factory=_factory(session),
    )
    await bridge.start()
    for _ in range(100):
        if bridge.list_tools():
            break
        await asyncio.sleep(0.01)
    return bridge


def _client(bridge):
    cfg = ConnectorClientConfig(server="wss://h/connector/ws", device_token="t")
    return ConnectorClient(cfg, mcp_bridge=bridge)


async def test_client_declares_mcp_capability(tmp_path):
    bridge = await _bridge_with(FakeSession([FakeTool("search")]))
    client = _client(bridge)

    class Rec:
        def __init__(self):
            self.sent = []

        async def send(self, data):
            self.sent.append(json.loads(data))

    rec = Rec()
    await client._register(rec)
    caps = rec.sent[0]["node"]["capabilities"]
    assert "mcp" in caps and "fs" in caps and "exec" in caps
    await bridge.stop()


async def test_client_mcp_list():
    bridge = await _bridge_with(FakeSession([FakeTool("search")]))
    client = _client(bridge)
    ws = FakeWS()
    await client._handle_rpc(ws, {"type": "rpc_request", "id": "1", "method": "mcp.list", "params": {}})
    resp = ws.frames[-1]
    assert resp["ok"] is True
    assert resp["result"]["tools"][0]["name"] == "search"
    await bridge.stop()


async def test_client_mcp_list_annotates_local_tools_with_live_arm_window():
    bridge = McpBridge(
        McpRegistry([McpServerDef(name="fs", command="x", approval="local")]),
        session_factory=_factory(FakeSession([FakeTool("search")])),
    )
    await bridge.start()
    for _ in range(100):
        if bridge.list_tools():
            break
        await asyncio.sleep(0.01)
    cfg = ConnectorClientConfig(server="wss://h/connector/ws", device_token="t")
    client = ConnectorClient(
        cfg, mcp_bridge=bridge,
        armed_remaining=lambda category: 600 if category == "mcp" else 0,
    )
    ws = FakeWS()
    await client._handle_rpc(ws, {"type": "rpc_request", "id": "1", "method": "mcp.list", "params": {}})
    tool = ws.frames[-1]["result"]["tools"][0]
    assert tool["approval"] == "local"
    assert tool["armedRemainingS"] == 600
    await bridge.stop()


async def test_client_mcp_call_forwards():
    bridge = await _bridge_with(FakeSession([FakeTool("search")], call_text="found it"))
    client = _client(bridge)
    ws = FakeWS()
    await client._handle_rpc(ws, {
        "type": "rpc_request", "id": "2", "method": "mcp.call",
        "params": {"server": "fs", "tool": "search", "args": {"q": "x"}},
    })
    await asyncio.gather(*list(client._exec_tasks))
    resp = ws.frames[-1]
    assert resp["ok"] is True
    assert resp["result"]["content"] == "found it"
    await bridge.stop()


async def test_client_mcp_call_unavailable_server():
    bridge = await _bridge_with(FakeSession([FakeTool("search")]))
    client = _client(bridge)
    ws = FakeWS()
    await client._handle_rpc(ws, {
        "type": "rpc_request", "id": "3", "method": "mcp.call",
        "params": {"server": "ghost", "tool": "x", "args": {}},
    })
    await asyncio.gather(*list(client._exec_tasks))
    resp = ws.frames[-1]
    assert resp["ok"] is False
    assert resp["error"]["code"] == "mcp_unavailable"
    await bridge.stop()


async def test_client_without_bridge_no_mcp_capability():
    cfg = ConnectorClientConfig(server="wss://h/connector/ws", device_token="t")
    client = ConnectorClient(cfg)  # no mcp.json → no bridge
    assert client.mcp_bridge is None

    class Rec:
        def __init__(self):
            self.sent = []

        async def send(self, data):
            self.sent.append(json.loads(data))

    rec = Rec()
    await client._register(rec)
    assert "mcp" not in rec.sent[0]["node"]["capabilities"]
