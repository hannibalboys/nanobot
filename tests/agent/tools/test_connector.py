"""Tests for connector_* agent tools (task 4.4)."""

from __future__ import annotations

import json

from nanobot.agent.tools.connector import (
    ConnectorCallMcpToolTool,
    ConnectorCallToolTool,
    ConnectorDesktopActTool,
    ConnectorDesktopEndTool,
    ConnectorDesktopSessionTool,
    ConnectorFetchFileTool,
    ConnectorListFilesTool,
    ConnectorListMcpToolsTool,
    ConnectorListNodesTool,
    ConnectorListToolsTool,
    ConnectorReadFileTool,
    _ConnectorDesktopTool,
    _ConnectorExecTool,
    _ConnectorMcpTool,
    _ConnectorTool,
)
from nanobot.agent.tools.context import RequestContext
from nanobot.agent.tools.loader import ToolLoader
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.config.schema import ConnectorConfig
from nanobot.connector import protocol as proto
from nanobot.connector.hub import ConnectorDisconnectedError, ConnectorError, ExecResult


class FakeHub:
    def __init__(self, *, nodes=None, rpc_result=None, rpc_error=None, fetch_dest=None, fetch_error=None):
        self._nodes = nodes or []
        self._rpc_result = rpc_result
        self._rpc_error = rpc_error
        self._fetch_dest = fetch_dest
        self._fetch_error = fetch_error
        self.rpc_calls = []
        self.fetch_calls = []

    def list_nodes(self, *, owner_id=None):
        return [n for n in self._nodes if owner_id is None or n.get("ownerId") == owner_id]

    async def rpc(self, node_id, method, params, *, timeout, owner_id=None):
        self.rpc_calls.append((node_id, method, params, owner_id))
        if self._rpc_error is not None:
            raise self._rpc_error
        return self._rpc_result

    async def fetch_file(self, node_id, path, *, base_dir, max_file_bytes, fetch_cache_max_bytes, timeout, owner_id=None, max_concurrent_transfers=2):
        self.fetch_calls.append((node_id, path, owner_id))
        if self._fetch_error is not None:
            raise self._fetch_error
        return self._fetch_dest


class _Ctx:
    def __init__(self, connector_config, workspace):
        self.connector_config = connector_config
        self.workspace = str(workspace)


def _tool(cls, hub, tmp_path):
    return cls(connector_config=ConnectorConfig(enabled=True), workspace=tmp_path, hub=hub)


def test_enabled_gating(tmp_path):
    assert _ConnectorTool.enabled(_Ctx(ConnectorConfig(enabled=True), tmp_path))
    assert not _ConnectorTool.enabled(_Ctx(ConnectorConfig(enabled=False), tmp_path))
    assert not _ConnectorTool.enabled(_Ctx(None, tmp_path))


def test_loader_registers_only_when_enabled(tmp_path):
    reg = ToolRegistry()
    ToolLoader().load(_Ctx(ConnectorConfig(enabled=True), tmp_path), reg)
    assert "connector_list_nodes" in reg
    assert "connector_fetch_file" in reg

    reg_off = ToolRegistry()
    ToolLoader().load(_Ctx(ConnectorConfig(enabled=False), tmp_path), reg_off)
    assert "connector_list_nodes" not in reg_off


async def test_list_nodes_empty_message(tmp_path):
    tool = _tool(ConnectorListNodesTool, FakeHub(nodes=[]), tmp_path)
    out = await tool.execute()
    assert "No local devices" in str(out)


async def test_list_nodes_returns_nodes(tmp_path):
    hub = FakeHub(nodes=[{"nodeId": "dev-1", "name": "PC", "ownerId": "webui"}])
    tool = _tool(ConnectorListNodesTool, hub, tmp_path)
    out = await tool.execute()
    assert "dev-1" in str(out)


async def test_list_nodes_includes_effective_gateway_capabilities(tmp_path):
    hub = FakeHub(nodes=[{"nodeId": "dev-1", "name": "PC", "ownerId": "webui"}])
    cfg = ConnectorConfig(
        enabled=True,
        allow_exec=True,
        allow_mcp_proxy=False,
        allow_desktop_control=True,
    )
    tool = ConnectorListNodesTool(connector_config=cfg, workspace=tmp_path, hub=hub)
    payload = json.loads(str(await tool.execute()))
    assert payload["effectiveCapabilities"] == {
        "fs": True,
        "exec": True,
        "mcp": False,
        "desktop": True,
    }

    provider = tool.runtime_context_provider()
    assert provider is not None
    block = await provider(RequestContext(channel="websocket", chat_id="chat"))
    assert "exec, desktop" in block.content
    assert "未启用能力为 mcp" in block.content


async def test_list_files_error_mapping(tmp_path):
    hub = FakeHub(rpc_error=ConnectorError(proto.ERROR_PATH_DENIED, "outside roots"))
    tool = _tool(ConnectorListFilesTool, hub, tmp_path)
    out = await tool.execute(node_id="dev-1", path="C:/secret")
    assert out.is_error
    assert "path_denied" in out
    assert "allow" in out  # actionable hint


async def test_offline_error_actionable(tmp_path):
    hub = FakeHub(rpc_error=ConnectorDisconnectedError())
    tool = _tool(ConnectorListFilesTool, hub, tmp_path)
    out = await tool.execute(node_id="dev-1", path="D:/x")
    assert out.is_error
    assert "offline" in out.lower()


async def test_read_file_inline(tmp_path):
    hub = FakeHub(rpc_result={"content": "hello text"})
    tool = _tool(ConnectorReadFileTool, hub, tmp_path)
    out = await tool.execute(node_id="dev-1", path="D:/a.txt")
    assert out == "hello text"


async def test_fetch_returns_server_path(tmp_path):
    landed = tmp_path / "connector" / "dev-1" / "a.bin"
    landed.parent.mkdir(parents=True)
    landed.write_bytes(b"payload")
    hub = FakeHub(fetch_dest=landed)
    tool = _tool(ConnectorFetchFileTool, hub, tmp_path)
    out = await tool.execute(node_id="dev-1", path="D:/a.bin")
    payload = json.loads(out)
    assert payload["server_path"] == str(landed)
    assert payload["bytes"] == 7
    # audit log written
    assert (tmp_path / "connector" / "audit.log").exists()


async def test_owner_scoping_passed_to_hub(tmp_path):
    hub = FakeHub(rpc_result={"entries": []})
    tool = _tool(ConnectorListFilesTool, hub, tmp_path)
    await tool.execute(node_id="dev-1", path="D:/x")
    assert hub.rpc_calls[0][3] == "webui"  # owner_id forwarded


# --- controlled execution (add-connector-local-tools) --------------------


class FakeCoordinator:
    def __init__(self, *, tools=None, result=None, error=None, mcp_tools=None, mcp_result=None):
        self._tools = tools if tools is not None else []
        self._result = result
        self._error = error
        self._mcp_tools = mcp_tools if mcp_tools is not None else []
        self._mcp_result = mcp_result if mcp_result is not None else {"content": "ok", "isError": False}
        self.calls = []
        self.mcp_calls = []

    async def list_tools(self, node_id, *, operator_id, owner_id):
        if self._error is not None:
            raise self._error
        return self._tools

    async def call_tool(self, node_id, tool, args, *, operator_id, owner_id,
                        on_output=None, cancel_event=None):
        self.calls.append((node_id, tool, args, operator_id, owner_id))
        if self._error is not None:
            raise self._error
        return self._result

    async def list_mcp_tools(self, node_id, *, operator_id, owner_id):
        if self._error is not None:
            raise self._error
        return self._mcp_tools

    async def call_mcp_tool(self, node_id, server, tool, args, *, operator_id, owner_id):
        self.mcp_calls.append((node_id, server, tool, args, operator_id, owner_id))
        if self._error is not None:
            raise self._error
        return self._mcp_result


def _exec_tool(cls, coordinator, tmp_path):
    return cls(
        connector_config=ConnectorConfig(enabled=True, allow_exec=True),
        workspace=tmp_path,
        coordinator=coordinator,
    )


def test_exec_tools_gated_on_allow_exec(tmp_path):
    on = _Ctx(ConnectorConfig(enabled=True, allow_exec=True), tmp_path)
    off = _Ctx(ConnectorConfig(enabled=True, allow_exec=False), tmp_path)
    assert _ConnectorExecTool.enabled(on)
    assert not _ConnectorExecTool.enabled(off)
    # file tools remain enabled regardless of allow_exec
    assert _ConnectorTool.enabled(off)


def test_loader_registers_exec_tools_only_with_allow_exec(tmp_path):
    reg = ToolRegistry()
    ToolLoader().load(_Ctx(ConnectorConfig(enabled=True, allow_exec=True), tmp_path), reg)
    assert "connector_list_tools" in reg
    assert "connector_call_tool" in reg

    reg_off = ToolRegistry()
    ToolLoader().load(_Ctx(ConnectorConfig(enabled=True, allow_exec=False), tmp_path), reg_off)
    assert "connector_list_tools" not in reg_off
    assert "connector_call_tool" not in reg_off
    # file tools still present
    assert "connector_list_nodes" in reg_off


async def test_list_tools_returns_schema(tmp_path):
    coord = FakeCoordinator(tools=[{"name": "open_notepad", "approval": "auto", "params": []}])
    tool = _exec_tool(ConnectorListToolsTool, coord, tmp_path)
    out = await tool.execute(node_id="dev-1")
    assert "open_notepad" in str(out)


async def test_list_tools_returns_approval_and_completion_semantics(tmp_path):
    coord = FakeCoordinator(tools=[{"name": "qq", "approval": "local", "params": []}])
    tool = _exec_tool(ConnectorListToolsTool, coord, tmp_path)
    payload = json.loads(str(await tool.execute(node_id="dev-1")))
    assert payload["tools"][0]["completion"] == "wait"
    assert "completionSafety" in payload["tools"][0]
    assert "本机限时预授权" in payload["approvalSemantics"]["local"]


async def test_list_tools_local_tool_shows_live_arm_state(tmp_path):
    coord = FakeCoordinator(tools=[
        {"name": "qq", "approval": "local", "params": [], "armedRemainingS": 1530},
        {"name": "google", "approval": "local", "params": [], "armedRemainingS": 0},
        {"name": "legacy", "approval": "local", "params": []},
        {"name": "junk", "approval": "local", "params": [], "armedRemainingS": "900"},
        {"name": "auto_tool", "approval": "auto", "params": []},
    ])
    tool = _exec_tool(ConnectorListToolsTool, coord, tmp_path)
    payload = json.loads(str(await tool.execute(node_id="dev-1")))
    tools = {t["name"]: t for t in payload["tools"]}
    assert tools["qq"]["localApprovalState"].startswith("armed (26m remaining)")
    assert "not armed" in tools["google"]["localApprovalState"]
    assert "arm exec" in tools["google"]["localApprovalState"]
    assert "unknown" in tools["legacy"]["localApprovalState"]
    # malformed wire data degrades to unknown instead of crashing
    assert "unknown" in tools["junk"]["localApprovalState"]
    # non-local policies carry no state field
    assert "localApprovalState" not in tools["auto_tool"]


async def test_list_tools_launch_completion_has_no_persistent_gui_warning(tmp_path):
    coord = FakeCoordinator(tools=[
        {"name": "qq", "approval": "local", "params": [], "completion": "launch"},
    ])
    tool = _exec_tool(ConnectorListToolsTool, coord, tmp_path)
    payload = json.loads(str(await tool.execute(node_id="dev-1")))

    assert "completionSafety" not in payload["tools"][0]


async def test_list_tools_empty_message(tmp_path):
    tool = _exec_tool(ConnectorListToolsTool, FakeCoordinator(tools=[]), tmp_path)
    out = await tool.execute(node_id="dev-1")
    assert "no registered tools" in str(out).lower()


async def test_list_tools_unsupported_device(tmp_path):
    coord = FakeCoordinator(error=ConnectorError(proto.ERROR_EXEC_UNSUPPORTED, "old client"))
    tool = _exec_tool(ConnectorListToolsTool, coord, tmp_path)
    out = await tool.execute(node_id="dev-1")
    assert out.is_error
    assert "update the connector" in str(out).lower()


async def test_call_tool_returns_result(tmp_path):
    result = ExecResult(exit_code=0, duration_ms=12, stdout="done\n", stderr="")
    coord = FakeCoordinator(result=result)
    tool = _exec_tool(ConnectorCallToolTool, coord, tmp_path)
    out = await tool.execute(node_id="dev-1", tool="open_notepad", args={"path": "a.txt"})
    payload = json.loads(out)
    assert payload["exitCode"] == 0
    assert payload["stdout"] == "done\n"
    assert coord.calls[0][:3] == ("dev-1", "open_notepad", {"path": "a.txt"})
    assert coord.calls[0][3] == "webui"  # operator == owner (self-use)


async def test_call_tool_denied_maps_error(tmp_path):
    coord = FakeCoordinator(error=ConnectorError(proto.ERROR_TOOL_NOT_FOUND, "nope"))
    tool = _exec_tool(ConnectorCallToolTool, coord, tmp_path)
    out = await tool.execute(node_id="dev-1", tool="ghost")
    assert out.is_error
    assert "connector_list_tools" in str(out)


async def test_call_tool_approval_denied_actionable(tmp_path):
    coord = FakeCoordinator(error=ConnectorError(proto.ERROR_APPROVAL_DENIED, "no"))
    tool = _exec_tool(ConnectorCallToolTool, coord, tmp_path)
    out = await tool.execute(node_id="dev-1", tool="risky")
    assert out.is_error
    assert "not approved" in str(out).lower()


async def test_exec_tool_without_coordinator_errors(tmp_path, monkeypatch):
    # Simulate a server where no gateway registered a coordinator.
    import nanobot.connector.exec as exec_mod

    monkeypatch.setattr(exec_mod, "_DEFAULT_COORDINATOR", None)
    tool = ConnectorCallToolTool(
        connector_config=ConnectorConfig(enabled=True, allow_exec=True),
        workspace=tmp_path,
    )
    assert tool._coordinator is None
    out = await tool.execute(node_id="dev-1", tool="x")
    assert out.is_error
    assert "allowexec" in str(out).lower()


def _mcp_tool(cls, coordinator, tmp_path):
    return cls(
        connector_config=ConnectorConfig(enabled=True, allow_exec=True, allow_mcp_proxy=True),
        workspace=tmp_path,
        coordinator=coordinator,
    )


def test_mcp_tools_gated_on_three_switches(tmp_path):
    all_on = _Ctx(ConnectorConfig(enabled=True, allow_exec=True, allow_mcp_proxy=True), tmp_path)
    no_mcp = _Ctx(ConnectorConfig(enabled=True, allow_exec=True, allow_mcp_proxy=False), tmp_path)
    no_exec = _Ctx(ConnectorConfig(enabled=True, allow_exec=False, allow_mcp_proxy=True), tmp_path)
    assert _ConnectorMcpTool.enabled(all_on)
    assert not _ConnectorMcpTool.enabled(no_mcp)
    assert not _ConnectorMcpTool.enabled(no_exec)


def test_loader_registers_mcp_tools_only_with_all_switches(tmp_path):
    reg = ToolRegistry()
    ToolLoader().load(_Ctx(ConnectorConfig(enabled=True, allow_exec=True, allow_mcp_proxy=True), tmp_path), reg)
    assert "connector_list_mcp_tools" in reg
    assert "connector_call_mcp_tool" in reg

    reg_off = ToolRegistry()
    ToolLoader().load(_Ctx(ConnectorConfig(enabled=True, allow_exec=True, allow_mcp_proxy=False), tmp_path), reg_off)
    assert "connector_list_mcp_tools" not in reg_off
    # exec tools still present when allow_exec on
    assert "connector_call_tool" in reg_off


async def test_list_mcp_tools_returns_schema(tmp_path):
    coord = FakeCoordinator(mcp_tools=[{"server": "fs", "name": "search", "approval": "auto"}])
    tool = _mcp_tool(ConnectorListMcpToolsTool, coord, tmp_path)
    out = await tool.execute(node_id="dev-1")
    assert "search" in str(out)


async def test_list_mcp_tools_local_tool_shows_live_arm_state(tmp_path):
    coord = FakeCoordinator(mcp_tools=[
        {"server": "fs", "name": "search", "approval": "local", "armedRemainingS": 600},
        {"server": "fs", "name": "legacy", "approval": "local"},
    ])
    tool = _mcp_tool(ConnectorListMcpToolsTool, coord, tmp_path)
    payload = json.loads(str(await tool.execute(node_id="dev-1")))
    tools = {t["name"]: t for t in payload["tools"]}
    assert tools["search"]["localApprovalState"].startswith("armed (10m remaining)")
    assert "unknown" in tools["legacy"]["localApprovalState"]


async def test_list_mcp_tools_empty_message(tmp_path):
    tool = _mcp_tool(ConnectorListMcpToolsTool, FakeCoordinator(mcp_tools=[]), tmp_path)
    out = await tool.execute(node_id="dev-1")
    assert "mcp add" in str(out).lower()


async def test_call_mcp_tool_forwards(tmp_path):
    coord = FakeCoordinator(mcp_result={"content": "done", "isError": False})
    tool = _mcp_tool(ConnectorCallMcpToolTool, coord, tmp_path)
    out = await tool.execute(node_id="dev-1", server="fs", tool="search", args={"q": "x"})
    payload = json.loads(out)
    assert payload["content"] == "done"
    assert coord.mcp_calls[0][:4] == ("dev-1", "fs", "search", {"q": "x"})


async def test_call_mcp_tool_unavailable_maps_error(tmp_path):
    coord = FakeCoordinator(error=ConnectorError(proto.ERROR_MCP_UNAVAILABLE, "down"))
    tool = _mcp_tool(ConnectorCallMcpToolTool, coord, tmp_path)
    out = await tool.execute(node_id="dev-1", server="fs", tool="search")
    assert out.is_error
    assert "not reachable" in str(out).lower()


class FakeDesktopManager:
    def __init__(self, *, start_result=None, act_result=None, error=None):
        self._start = start_result if start_result is not None else {
            "sessionId": "sess-1", "image": "aW1n", "width": 800, "height": 600,
        }
        self._act = act_result if act_result is not None else {"image": "aW1n", "width": 800, "height": 600}
        self._error = error
        self.calls = []

    async def start(self, node_id, *, operator_id, owner_id, goal):
        self.calls.append(("start", node_id, goal, operator_id))
        if self._error is not None:
            raise self._error
        return self._start

    async def act(self, session_id, action, *, operator_id):
        self.calls.append(("act", session_id, action))
        if self._error is not None:
            raise self._error
        return self._act

    async def end(self, session_id, *, operator_id):
        self.calls.append(("end", session_id))
        return {"ended": True}


def _desktop_tool(cls, manager, tmp_path):
    return cls(
        connector_config=ConnectorConfig(enabled=True, allow_desktop_control=True),
        workspace=tmp_path,
        manager=manager,
    )


def test_desktop_tools_gated_on_two_switches(tmp_path):
    on = _Ctx(ConnectorConfig(enabled=True, allow_desktop_control=True), tmp_path)
    off = _Ctx(ConnectorConfig(enabled=True, allow_desktop_control=False), tmp_path)
    assert _ConnectorDesktopTool.enabled(on)
    assert not _ConnectorDesktopTool.enabled(off)
    # independent of allow_exec
    exec_only = _Ctx(ConnectorConfig(enabled=True, allow_exec=True, allow_desktop_control=False), tmp_path)
    assert not _ConnectorDesktopTool.enabled(exec_only)


def test_loader_registers_desktop_tools_only_with_switch(tmp_path):
    reg = ToolRegistry()
    ToolLoader().load(_Ctx(ConnectorConfig(enabled=True, allow_desktop_control=True), tmp_path), reg)
    assert "connector_desktop_session" in reg
    assert "connector_desktop_act" in reg
    assert "connector_desktop_end" in reg

    reg_off = ToolRegistry()
    ToolLoader().load(_Ctx(ConnectorConfig(enabled=True, allow_desktop_control=False), tmp_path), reg_off)
    assert "connector_desktop_session" not in reg_off


async def test_desktop_session_returns_first_frame(tmp_path):
    mgr = FakeDesktopManager()
    tool = _desktop_tool(ConnectorDesktopSessionTool, mgr, tmp_path)
    out = await tool.execute(node_id="dev-1", goal="open app")
    assert isinstance(out, list)
    assert out[0]["type"] == "image_url"
    assert out[0]["image_url"]["url"] == "data:image/png;base64,aW1n"
    assert "session_id: sess-1" in out[1]["text"]
    assert "aW1n" not in out[1]["text"]
    assert mgr.calls[0][:3] == ("start", "dev-1", "open app")


async def test_desktop_act_returns_next_frame(tmp_path):
    mgr = FakeDesktopManager()
    tool = _desktop_tool(ConnectorDesktopActTool, mgr, tmp_path)
    out = await tool.execute(session_id="sess-1", action={"type": "click", "x": 1, "y": 2})
    assert isinstance(out, list)
    assert out[0]["type"] == "image_url"
    assert out[0]["image_url"]["url"] == "data:image/png;base64,aW1n"
    assert "session_id: sess-1" in out[1]["text"]
    assert "800x600" in out[1]["text"]
    assert mgr.calls[0] == ("act", "sess-1", {"type": "click", "x": 1, "y": 2})


async def test_desktop_session_denied_maps_error(tmp_path):
    mgr = FakeDesktopManager(error=ConnectorError(proto.ERROR_SESSION_ENDED, "not authorized"))
    tool = _desktop_tool(ConnectorDesktopSessionTool, mgr, tmp_path)
    out = await tool.execute(node_id="dev-1", goal="x")
    assert out.is_error
    assert "session ended" in str(out).lower()


async def test_desktop_act_sensitive_unconfirmed_maps_error(tmp_path):
    mgr = FakeDesktopManager(error=ConnectorError(proto.ERROR_SENSITIVE_UNCONFIRMED, "blocked"))
    tool = _desktop_tool(ConnectorDesktopActTool, mgr, tmp_path)
    out = await tool.execute(session_id="s", action={"type": "click", "label": "Pay"})
    assert out.is_error
    assert "sensitive" in str(out).lower()


async def test_desktop_end(tmp_path):
    mgr = FakeDesktopManager()
    tool = _desktop_tool(ConnectorDesktopEndTool, mgr, tmp_path)
    out = await tool.execute(session_id="sess-1")
    assert json.loads(out)["ended"] is True


async def test_desktop_tool_without_manager_errors(tmp_path, monkeypatch):
    import nanobot.connector.desktop as desk_mod

    monkeypatch.setattr(desk_mod, "_DEFAULT_DESKTOP_MANAGER", None)
    tool = ConnectorDesktopSessionTool(
        connector_config=ConnectorConfig(enabled=True, allow_desktop_control=True),
        workspace=tmp_path,
    )
    assert tool._manager is None
    out = await tool.execute(node_id="dev-1", goal="x")
    assert out.is_error
    assert "allowdesktopcontrol" in str(out).lower()


async def test_list_nodes_includes_alias(tmp_path):
    from nanobot.connector.devices import DeviceStore

    store = DeviceStore(tmp_path / "connector" / "devices.json")
    code, _ = store.generate_pairing_code("webui")
    node_id, _tok = store.redeem_pairing_code(
        code, name="PC", platform="windows", machine_fingerprint="fp1"
    )
    store.set_alias(node_id, "Zhang's Laptop", owner_id="webui")

    hub = FakeHub(nodes=[{"nodeId": node_id, "name": "PC", "ownerId": "webui"}])
    tool = _tool(ConnectorListNodesTool, hub, tmp_path)
    out = await tool.execute()
    assert "Zhang's Laptop" in str(out)
