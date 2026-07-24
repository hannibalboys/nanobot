"""Client-side exec dispatch: tools.list / tools.call / tools.cancel end to end."""

from __future__ import annotations

import asyncio
import json
import sys

import pytest

from nanobot_connector.client import ConnectorClient
from nanobot_connector.config import ConnectorClientConfig
from nanobot_connector.tools import ToolDef, ToolRegistry


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("NANOBOT_CONNECTOR_HOME", str(tmp_path / "home"))


class FakeWS:
    def __init__(self) -> None:
        self.frames: list[dict] = []

    async def send(self, data: str) -> None:
        self.frames.append(json.loads(data))


def _print_tool(text: str = "hello", approval: str = "auto") -> ToolDef:
    return ToolDef.model_validate({
        "name": "printer",
        "exec": sys.executable,
        "argv": ["-c", f"print({text!r})"],
        "approval": approval,
    })


def _sleep_tool(approval: str = "auto") -> ToolDef:
    return ToolDef.model_validate({
        "name": "sleeper",
        "exec": sys.executable,
        "argv": ["-c", "import time; time.sleep(30)"],
        "approval": approval,
    })


def _client(tools, **kwargs) -> ConnectorClient:
    cfg = ConnectorClientConfig(server="wss://h/connector/ws", device_token="t")
    return ConnectorClient(cfg, registry=ToolRegistry(tools), **kwargs)


async def _call(client: ConnectorClient, ws: FakeWS, rpc_id: str, tool: str, args=None):
    await client._handle_rpc(ws, {
        "type": "rpc_request", "id": rpc_id, "method": "tools.call",
        "params": {"tool": tool, "args": args or {}},
    })
    if client._exec_tasks:
        await asyncio.gather(*list(client._exec_tasks))


async def test_tools_list_returns_public_schema():
    client = _client([_print_tool()])
    ws = FakeWS()
    await client._handle_rpc(ws, {"type": "rpc_request", "id": "1", "method": "tools.list", "params": {}})
    resp = ws.frames[-1]
    assert resp["type"] == "rpc_response" and resp["ok"] is True
    names = [t["name"] for t in resp["result"]["tools"]]
    assert names == ["printer"]


async def test_tools_list_annotates_local_tools_with_live_arm_window():
    client = _client(
        [_print_tool(approval="local"), _sleep_tool(approval="auto")],
        armed_remaining=lambda category: 900 if category == "exec" else 0,
    )
    ws = FakeWS()
    await client._handle_rpc(ws, {"type": "rpc_request", "id": "1", "method": "tools.list", "params": {}})
    tools = {t["name"]: t for t in ws.frames[-1]["result"]["tools"]}
    # approval=local tools carry the live arm window; other policies do not
    assert tools["printer"]["armedRemainingS"] == 900
    assert "armedRemainingS" not in tools["sleeper"]


async def test_tools_list_survives_an_arm_status_read_failure():
    def unavailable(_category: str) -> int:
        raise OSError("arm state unreadable")

    client = _client([_print_tool(approval="local")], armed_remaining=unavailable)
    ws = FakeWS()
    await client._handle_rpc(ws, {"type": "rpc_request", "id": "1", "method": "tools.list", "params": {}})

    tool = ws.frames[-1]["result"]["tools"][0]
    assert tool["name"] == "printer"
    assert "armedRemainingS" not in tool


async def test_tools_list_without_arm_hook_reports_no_window():
    client = _client([_print_tool(approval="local")])  # no armed_remaining hook
    ws = FakeWS()
    await client._handle_rpc(ws, {"type": "rpc_request", "id": "1", "method": "tools.list", "params": {}})
    assert "armedRemainingS" not in ws.frames[-1]["result"]["tools"][0]


async def test_call_auto_streams_output_then_result():
    client = _client([_print_tool("hi there")])
    ws = FakeWS()
    await _call(client, ws, "r1", "printer")
    outputs = [f for f in ws.frames if f["type"] == "exec_output"]
    results = [f for f in ws.frames if f["type"] == "exec_result"]
    assert "hi there" in "".join(f["data"] for f in outputs)
    assert len(results) == 1
    assert results[0]["exitCode"] == 0
    assert results[0]["cancelled"] is False


async def test_call_unknown_tool_is_prestart_failure():
    client = _client([])
    ws = FakeWS()
    await _call(client, ws, "r1", "ghost")
    resp = ws.frames[-1]
    assert resp["type"] == "rpc_response" and resp["ok"] is False
    assert resp["error"]["code"] == "tool_not_found"
    # a pre-start failure emits no exec_result
    assert not any(f["type"] == "exec_result" for f in ws.frames)


async def test_call_invalid_args_rejected():
    tool = ToolDef.model_validate({
        "name": "printer", "exec": sys.executable, "argv": ["-c", "print(1)"],
        "params": [{"name": "n", "type": "int", "required": True}], "approval": "auto",
    })
    client = _client([tool])
    ws = FakeWS()
    await _call(client, ws, "r1", "printer", {"n": "not-int"})
    resp = ws.frames[-1]
    assert resp["ok"] is False and resp["error"]["code"] == "invalid_args"


async def test_local_approval_denied_without_handler():
    client = _client([_print_tool(approval="local")])  # no approval hook
    ws = FakeWS()
    await _call(client, ws, "r1", "printer")
    resp = ws.frames[-1]
    assert resp["ok"] is False and resp["error"]["code"] == "approval_denied"


async def test_local_approval_allowed_with_handler():
    async def approve(tool, args):
        return True

    client = _client([_print_tool(approval="local")], on_local_approval=approve)
    ws = FakeWS()
    await _call(client, ws, "r1", "printer")
    assert any(f["type"] == "exec_result" and f["exitCode"] == 0 for f in ws.frames)


async def test_launch_completion_returns_without_waiting_for_gui_process():
    tool = ToolDef.model_validate({
        "name": "browser",
        "exec": sys.executable,
        "argv": ["-c", "import time; time.sleep(3)"],
        "approval": "auto",
        "completion": "launch",
    })
    client = _client([tool])
    ws = FakeWS()
    start = asyncio.get_running_loop().time()
    await _call(client, ws, "r1", "browser")
    assert asyncio.get_running_loop().time() - start < 1.0
    result = [frame for frame in ws.frames if frame["type"] == "exec_result"][-1]
    assert result["exitCode"] == 0 and result["cancelled"] is False


async def test_unexpected_executor_error_returns_rpc_error(monkeypatch):
    async def fail_execution(*_args, **_kwargs):
        raise RuntimeError("simulated executor failure")

    monkeypatch.setattr("nanobot_connector.client.run_execution", fail_execution)
    client = _client([_print_tool()])
    ws = FakeWS()
    await _call(client, ws, "r1", "printer")
    response = ws.frames[-1]
    assert response["type"] == "rpc_response" and response["ok"] is False
    assert response["error"]["code"] == "internal"
    assert not any(frame["type"] == "exec_result" for frame in ws.frames)


async def test_cancel_terminates_running_exec():
    client = _client([_sleep_tool()])
    ws = FakeWS()
    await client._handle_rpc(ws, {
        "type": "rpc_request", "id": "r1", "method": "tools.call",
        "params": {"tool": "sleeper", "args": {}},
    })
    await asyncio.sleep(0.3)  # let the process actually start
    await client._dispatch(ws, {"type": "cancel", "id": "r1"})
    await asyncio.gather(*list(client._exec_tasks))
    result = [f for f in ws.frames if f["type"] == "exec_result"][-1]
    assert result["cancelled"] is True
