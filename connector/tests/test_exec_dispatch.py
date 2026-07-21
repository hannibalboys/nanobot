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
