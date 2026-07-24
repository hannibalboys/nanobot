"""Tests for the client-side local MCP bridge (add-connector-mcp-proxy)."""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from nanobot_connector.mcp_bridge import McpBridge, McpRegistry, McpServerDef
from nanobot_connector.persistence import LocalStateConflictError, LocalStateError


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("NANOBOT_CONNECTOR_HOME", str(tmp_path / "home"))


class FakeTool:
    def __init__(self, name, description="", input_schema=None):
        self.name = name
        self.description = description
        self.inputSchema = input_schema or {}


class FakeListResult:
    def __init__(self, tools):
        self.tools = tools


class FakeContent:
    def __init__(self, text):
        self.text = text


class FakeCallResult:
    def __init__(self, text, is_error=False):
        self.content = [FakeContent(text)]
        self.isError = is_error


class FakeSession:
    def __init__(self, tools, call_text="ok"):
        self._tools = tools
        self._call_text = call_text
        self.calls = []

    async def initialize(self):
        return None

    async def list_tools(self):
        return FakeListResult(self._tools)

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return FakeCallResult(self._call_text)


def _factory(session, *, fail_times=0):
    state = {"n": 0}

    @contextlib.asynccontextmanager
    async def factory(sdef, env):
        state["n"] += 1
        if state["n"] <= fail_times:
            raise RuntimeError("cannot start")
        yield session

    return factory


async def _wait_tools(bridge, *, want=True, tries=100):
    for _ in range(tries):
        if bool(bridge.list_tools()) == want:
            return
        await asyncio.sleep(0.01)


async def test_bridge_lists_and_calls_tools():
    session = FakeSession([FakeTool("search", "find files")], call_text="hit")
    bridge = McpBridge(
        McpRegistry([McpServerDef(name="fs", command="x", approval="auto")]),
        session_factory=_factory(session),
    )
    await bridge.start()
    await _wait_tools(bridge)
    tools = bridge.list_tools()
    assert tools[0]["name"] == "search"
    assert tools[0]["server"] == "fs"
    assert tools[0]["approval"] == "auto"
    assert bridge.approval_for("fs") == "auto"

    result = await bridge.call_tool("fs", "search", {"q": "report"})
    assert result["content"] == "hit"
    assert result["isError"] is False
    assert session.calls == [("search", {"q": "report"})]
    await bridge.stop()


async def test_bridge_local_approval_fail_closed_without_handler():
    session = FakeSession([FakeTool("x")])  # default approval="local"
    bridge = McpBridge(
        McpRegistry([McpServerDef(name="s", command="x")]),  # local, no handler
        session_factory=_factory(session),
    )
    await bridge.start()
    await _wait_tools(bridge)
    with pytest.raises(PermissionError):
        await bridge.call_tool("s", "x", {})
    await bridge.stop()


async def test_bridge_local_approval_with_handler():
    session = FakeSession([FakeTool("x")], call_text="ran")
    seen = []

    async def approve(server, tool, args):
        seen.append((server, tool))
        return True

    bridge = McpBridge(
        McpRegistry([McpServerDef(name="s", command="x")]),  # local
        session_factory=_factory(session),
        on_local_approval=approve,
    )
    await bridge.start()
    await _wait_tools(bridge)
    result = await bridge.call_tool("s", "x", {})
    assert result["content"] == "ran"
    assert seen == [("s", "x")]
    await bridge.stop()


async def test_bridge_local_approval_denied_by_handler():
    session = FakeSession([FakeTool("x")])

    async def deny(server, tool, args):
        return False

    bridge = McpBridge(
        McpRegistry([McpServerDef(name="s", command="x")]),
        session_factory=_factory(session),
        on_local_approval=deny,
    )
    await bridge.start()
    await _wait_tools(bridge)
    with pytest.raises(PermissionError):
        await bridge.call_tool("s", "x", {})
    await bridge.stop()


async def test_bridge_enabled_tools_filter():
    session = FakeSession([FakeTool("a"), FakeTool("b"), FakeTool("c")])
    sdef = McpServerDef(name="s", command="x", enabled_tools=["a", "c"])
    bridge = McpBridge(McpRegistry([sdef]), session_factory=_factory(session))
    await bridge.start()
    await _wait_tools(bridge)
    names = {t["name"] for t in bridge.list_tools()}
    assert names == {"a", "c"}
    await bridge.stop()


async def test_bridge_unavailable_server_call_raises():
    session = FakeSession([FakeTool("x")])
    bridge = McpBridge(
        McpRegistry([McpServerDef(name="s", command="x")]),
        session_factory=_factory(session, fail_times=99),  # never connects
        reconnect_interval_s=0.05,
    )
    await bridge.start()
    await asyncio.sleep(0.1)
    assert bridge.list_tools() == []
    with pytest.raises(ConnectionError):
        await bridge.call_tool("s", "x", {})
    health = {h["server"]: h for h in bridge.server_health()}
    assert health["s"]["healthy"] is False
    await bridge.stop()


async def test_bridge_reconnects_after_failure():
    session = FakeSession([FakeTool("x")])
    bridge = McpBridge(
        McpRegistry([McpServerDef(name="s", command="x")]),
        session_factory=_factory(session, fail_times=1),  # fail once, then succeed
        reconnect_interval_s=0.05,
    )
    await bridge.start()
    await _wait_tools(bridge, tries=200)  # health loop restarts it
    assert [t["name"] for t in bridge.list_tools()] == ["x"]
    await bridge.stop()


def test_registry_roundtrip_and_malformed_skip(tmp_path):
    import json

    path = tmp_path / "mcp.json"
    reg = McpRegistry([McpServerDef(name="fs", command="x")], path=path)
    reg.save()
    assert path.exists()

    # inject a malformed entry alongside the valid one
    data = json.loads(path.read_text(encoding="utf-8"))
    data["servers"].append({"bogus": True})
    path.write_text(json.dumps(data), encoding="utf-8")

    reloaded = McpRegistry.load(path=path)
    assert [s.name for s in reloaded.list()] == ["fs"]


def test_registry_save_rejects_stale_snapshot(tmp_path):
    path = tmp_path / "mcp.json"
    first = McpRegistry.load(path=path)
    second = McpRegistry.load(path=path)

    first.add(McpServerDef(name="one", command="one"))
    first.save()
    second.add(McpServerDef(name="two", command="two"))

    with pytest.raises(LocalStateConflictError, match="其他连接器进程"):
        second.save()
    assert [server.name for server in McpRegistry.load(path=path).list()] == ["one"]


def test_malformed_registry_document_is_rejected_without_overwrite(tmp_path):
    path = tmp_path / "mcp.json"
    path.write_text("{", encoding="utf-8")

    with pytest.raises(LocalStateError, match="有效 JSON"):
        McpRegistry.load(path=path)
    assert path.read_text(encoding="utf-8") == "{"


async def test_bridge_request_after_stop_fails_fast():
    """A call racing with disconnect must fail fast, never hang on a dead worker."""
    session = FakeSession([FakeTool("x")], )
    sdef = McpServerDef(name="s", command="x", approval="auto")
    bridge = McpBridge(McpRegistry([sdef]), session_factory=_factory(session))
    await bridge.start()
    await _wait_tools(bridge)
    conn = bridge._conns["s"]
    # Kill the worker task out from under a request (simulates disconnect race).
    await conn.stop()
    with pytest.raises(ConnectionError):
        await asyncio.wait_for(bridge.call_tool("s", "x", {}), timeout=2)
    await bridge.stop()


def test_transport_autodetect():
    assert McpServerDef(name="a", command="x").transport() == "stdio"
    assert McpServerDef(name="b", url="http://h/sse").transport() == "sse"
    assert McpServerDef(name="c", url="http://h/mcp").transport() == "streamableHttp"
