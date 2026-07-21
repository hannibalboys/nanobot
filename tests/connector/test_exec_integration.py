"""Integration tests for controlled execution: hub routing + coordinator (v2)."""

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
    summarize_args,
)
from nanobot.connector.hub import ConnectorError, ConnectorHub

from .conftest import FakeConn, duplex


class FakeExecConnector:
    """A connector that supports exec: answers tools.list and streams tools.call."""

    def __init__(
        self,
        conn: FakeConn,
        *,
        tools: list[dict],
        outputs: list[tuple[str, str]] | None = None,
        exit_code: int = 0,
        capabilities: list[str] | None = None,
        per_chunk_delay: float = 0.0,
    ) -> None:
        self.conn = conn
        self.tools = tools
        self.outputs = outputs if outputs is not None else [("stdout", "hello\n")]
        self.exit_code = exit_code
        self.capabilities = capabilities if capabilities is not None else [proto.CAP_FS, proto.CAP_EXEC]
        self.per_chunk_delay = per_chunk_delay
        self.cancelled: set[str] = set()
        self._exec_tasks: set[asyncio.Task] = set()

    async def run(self):
        await self.conn.send(json.dumps(proto.dump_frame(proto.RegisterFrame(
            node=proto.NodeInfo(name="fake-exec", platform="test", capabilities=self.capabilities),
        ))))
        async for raw in self.conn:
            frame = proto.parse_frame(json.loads(raw))
            if isinstance(frame, proto.RegisteredFrame):
                continue
            if isinstance(frame, proto.CancelFrame):
                self.cancelled.add(frame.id)
                continue
            if isinstance(frame, proto.RpcRequestFrame):
                await self._handle(frame)

    async def _handle(self, frame: proto.RpcRequestFrame):
        if frame.method == "tools.list":
            await self._respond(frame.id, {"tools": self.tools})
            return
        if frame.method == "tools.call":
            name = frame.params.get("tool")
            if name not in {t["name"] for t in self.tools}:
                await self._error(frame.id, proto.ERROR_TOOL_NOT_FOUND, "no such tool")
                return
            task = asyncio.create_task(self._run_exec(frame.id))
            self._exec_tasks.add(task)
            task.add_done_callback(self._exec_tasks.discard)
            return
        await self._error(frame.id, proto.ERROR_INTERNAL, "unknown method")

    async def _run_exec(self, rpc_id: str):
        seq = 0
        for stream, data in self.outputs:
            if rpc_id in self.cancelled:
                await self._send(proto.ExecResultFrame(id=rpc_id, exit_code=None, duration_ms=1, cancelled=True))
                return
            await self._send(proto.ExecOutputFrame(id=rpc_id, stream=stream, seq=seq, data=data))
            seq += 1
            if self.per_chunk_delay:
                await asyncio.sleep(self.per_chunk_delay)
        if rpc_id in self.cancelled:
            await self._send(proto.ExecResultFrame(id=rpc_id, exit_code=None, duration_ms=1, cancelled=True))
            return
        await self._send(proto.ExecResultFrame(id=rpc_id, exit_code=self.exit_code, duration_ms=5))

    async def _respond(self, rpc_id, result):
        await self._send(proto.RpcResponseFrame(id=rpc_id, ok=True, result=result))

    async def _error(self, rpc_id, code, message):
        await self._send(proto.RpcResponseFrame(id=rpc_id, ok=False, error={"code": code, "message": message}))

    async def _send(self, frame):
        await self.conn.send(json.dumps(proto.dump_frame(frame)))


async def _online(**kwargs):
    server_conn, client_conn = duplex()
    hub = ConnectorHub()
    connector = FakeExecConnector(client_conn, **kwargs)
    serve_task = asyncio.create_task(hub.serve(server_conn, node_id="dev-1", owner_id="webui"))
    ctask = asyncio.create_task(connector.run())
    for _ in range(200):
        if hub.list_nodes():
            break
        await asyncio.sleep(0.01)
    return hub, serve_task, ctask, connector, server_conn


_TOOL = {"name": "printer", "approval": "auto", "params": []}


# --- hub-level -----------------------------------------------------------


async def test_list_tools_over_hub():
    hub, serve_task, ctask, _c, sconn = await _online(tools=[_TOOL])
    tools = await hub.list_tools("dev-1", timeout=2)
    assert [t["name"] for t in tools] == ["printer"]
    await sconn.close()
    await asyncio.gather(serve_task, ctask)


async def test_call_tool_streams_and_result():
    seen = []

    async def on_output(stream, text, seq):
        seen.append((stream, text))

    hub, serve_task, ctask, _c, sconn = await _online(
        tools=[_TOOL], outputs=[("stdout", "a"), ("stdout", "b")], exit_code=0
    )
    result = await hub.call_tool(
        "dev-1", "printer", {}, timeout=3, max_output_bytes=1000, on_output=on_output
    )
    assert result.exit_code == 0
    assert result.stdout == "ab"
    assert seen == [("stdout", "a"), ("stdout", "b")]
    await sconn.close()
    await asyncio.gather(serve_task, ctask)


async def test_call_tool_exec_unsupported():
    hub, serve_task, ctask, _c, sconn = await _online(tools=[], capabilities=[proto.CAP_FS])
    with pytest.raises(ConnectorError) as ei:
        await hub.call_tool("dev-1", "printer", {}, timeout=2, max_output_bytes=1000)
    assert ei.value.code == proto.ERROR_EXEC_UNSUPPORTED
    await sconn.close()
    await asyncio.gather(serve_task, ctask)


async def test_call_tool_concurrency_limit():
    hub, serve_task, ctask, _c, sconn = await _online(tools=[_TOOL])
    node = hub._nodes["dev-1"]
    node._execs["busy"] = type("E", (), {"queue": asyncio.Queue()})()
    with pytest.raises(ConnectorError) as ei:
        await hub.call_tool(
            "dev-1", "printer", {}, timeout=2, max_output_bytes=1000, max_concurrent_execs=1
        )
    assert ei.value.code == proto.ERROR_EXEC_LIMIT
    await sconn.close()
    await asyncio.gather(serve_task, ctask)


async def test_call_tool_cancel():
    hub, serve_task, ctask, connector, sconn = await _online(
        tools=[_TOOL], outputs=[("stdout", f"{i}") for i in range(50)], per_chunk_delay=0.05
    )
    cancel = asyncio.Event()

    async def canceller():
        await asyncio.sleep(0.2)
        cancel.set()

    tk = asyncio.create_task(canceller())
    result = await hub.call_tool(
        "dev-1", "printer", {}, timeout=5, max_output_bytes=100000, cancel_event=cancel
    )
    await tk
    assert result.cancelled is True
    await sconn.close()
    await asyncio.gather(serve_task, ctask)


async def test_call_tool_output_truncation():
    hub, serve_task, ctask, _c, sconn = await _online(
        tools=[_TOOL], outputs=[("stdout", "X" * 100)]
    )
    result = await hub.call_tool("dev-1", "printer", {}, timeout=3, max_output_bytes=10)
    assert result.truncated is True
    assert len(result.stdout) <= 10
    await sconn.close()
    await asyncio.gather(serve_task, ctask)


# --- coordinator-level ---------------------------------------------------


def _cfg(**over):
    base = dict(
        rpc_timeout_s=2, exec_timeout_s=3, max_exec_output_bytes=10_000,
        max_concurrent_execs=2, approval_ttl_s=1, exec_rate_per_minute=100,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _coord(hub, tmp_path, **cfg_over):
    # Fresh metrics/broker/limiter per coordinator so tests don't share the
    # process-global singletons.
    cfg = _cfg(**cfg_over)
    return ExecutionCoordinator(
        hub=hub,
        authz=AuthorizationStore(tmp_path / "grants.json"),
        workspace=tmp_path,
        config=cfg,
        metrics=ExecMetrics(),
        broker=ApprovalBroker(),
        rate_limiter=RateLimiter(per_minute=cfg.exec_rate_per_minute),
    )


async def test_coordinator_self_use_runs(tmp_path):
    hub, serve_task, ctask, _c, sconn = await _online(tools=[_TOOL])
    coord = _coord(hub, tmp_path)
    result = await coord.call_tool("dev-1", "printer", {}, operator_id="webui", owner_id="webui")
    assert result.exit_code == 0
    assert coord._metrics.snapshot()["executions"] == 1
    await sconn.close()
    await asyncio.gather(serve_task, ctask)


async def test_coordinator_cross_person_denied_as_not_found(tmp_path):
    hub, serve_task, ctask, _c, sconn = await _online(tools=[_TOOL])
    coord = _coord(hub, tmp_path)
    with pytest.raises(ConnectorError) as ei:
        await coord.call_tool("dev-1", "printer", {}, operator_id="alice", owner_id="webui")
    # existence hidden: reported as not-found, not "denied"
    assert ei.value.code == proto.ERROR_TOOL_NOT_FOUND
    await sconn.close()
    await asyncio.gather(serve_task, ctask)


async def test_coordinator_cross_person_allowed_after_grant(tmp_path):
    hub, serve_task, ctask, _c, sconn = await _online(tools=[_TOOL])
    coord = _coord(hub, tmp_path)
    coord._authz.grant("dev-1", "printer", "alice", granted_by="webui")
    result = await coord.call_tool("dev-1", "printer", {}, operator_id="alice", owner_id="webui")
    assert result.exit_code == 0
    await sconn.close()
    await asyncio.gather(serve_task, ctask)


async def test_coordinator_rate_limit(tmp_path):
    hub, serve_task, ctask, _c, sconn = await _online(tools=[_TOOL])
    coord = _coord(hub, tmp_path, exec_rate_per_minute=1)
    await coord.call_tool("dev-1", "printer", {}, operator_id="webui", owner_id="webui")
    with pytest.raises(ConnectorError) as ei:
        await coord.call_tool("dev-1", "printer", {}, operator_id="webui", owner_id="webui")
    assert ei.value.code == proto.ERROR_EXEC_LIMIT
    assert coord._metrics.snapshot()["rateLimited"] == 1
    await sconn.close()
    await asyncio.gather(serve_task, ctask)


async def test_coordinator_webui_approval_default_deny_on_timeout(tmp_path):
    hub, serve_task, ctask, _c, sconn = await _online(
        tools=[{"name": "printer", "approval": "webui", "params": []}]
    )
    coord = _coord(hub, tmp_path, approval_ttl_s=1)
    with pytest.raises(ConnectorError) as ei:
        await coord.call_tool("dev-1", "printer", {}, operator_id="webui", owner_id="webui")
    assert ei.value.code == proto.ERROR_APPROVAL_DENIED
    assert coord._metrics.snapshot()["approvalDenied"] == 1
    await sconn.close()
    await asyncio.gather(serve_task, ctask)


async def test_coordinator_webui_approval_granted(tmp_path):
    hub, serve_task, ctask, _c, sconn = await _online(
        tools=[{"name": "printer", "approval": "webui", "params": []}]
    )
    coord = _coord(hub, tmp_path, approval_ttl_s=5)

    async def approve_soon():
        for _ in range(100):
            pending = coord._broker.list_pending()
            if pending:
                await coord._broker.resolve(pending[0]["approvalId"], approved=True)
                return
            await asyncio.sleep(0.02)

    tk = asyncio.create_task(approve_soon())
    result = await coord.call_tool("dev-1", "printer", {}, operator_id="webui", owner_id="webui")
    await tk
    assert result.exit_code == 0
    await sconn.close()
    await asyncio.gather(serve_task, ctask)


def test_rate_limiter_unit():
    async def go():
        rl = RateLimiter(per_minute=2)
        assert await rl.check_and_record("k")
        assert await rl.check_and_record("k")
        assert not await rl.check_and_record("k")
        assert await rl.check_and_record("other")

    asyncio.run(go())


# --- review fixes --------------------------------------------------------


async def test_list_tools_cross_person_filtered_by_grant(tmp_path):
    """Fix C: a cross-person operator only sees tools they were granted."""
    hub, serve_task, ctask, _c, sconn = await _online(tools=[
        {"name": "printer", "approval": "auto", "params": []},
        {"name": "secret_tool", "approval": "auto", "params": []},
    ])
    coord = _coord(hub, tmp_path)
    coord._authz.grant("dev-1", "printer", "alice", granted_by="webui")

    owner_view = await coord.list_tools("dev-1", operator_id="webui", owner_id="webui")
    assert {t["name"] for t in owner_view} == {"printer", "secret_tool"}

    alice_view = await coord.list_tools("dev-1", operator_id="alice", owner_id="webui")
    assert {t["name"] for t in alice_view} == {"printer"}  # secret_tool hidden

    await sconn.close()
    await asyncio.gather(serve_task, ctask)


async def test_exec_disconnect_fails_running_execution():
    """Fix (6.2): a device dropping mid-run fails the execution with node_offline,
    not a hang. Exercises _fail_node draining in-flight execs on detach."""
    hub, serve_task, ctask, connector, sconn = await _online(
        tools=[_TOOL], outputs=[("stdout", f"{i}") for i in range(50)], per_chunk_delay=0.05
    )
    call = asyncio.create_task(
        hub.call_tool("dev-1", "printer", {}, timeout=10, max_output_bytes=100000)
    )
    await asyncio.sleep(0.15)  # let the execution start streaming
    await sconn.close()  # device drops mid-run
    with pytest.raises(ConnectorError) as ei:
        await call
    assert ei.value.code == proto.ERROR_NODE_OFFLINE
    await asyncio.gather(serve_task, ctask, return_exceptions=True)


async def test_call_tool_cancellation_sends_cancel_to_device(tmp_path):
    """Fix B: cancelling the call (e.g. aborted turn) must tell the device to stop."""
    hub, serve_task, ctask, connector, sconn = await _online(
        tools=[_TOOL], outputs=[("stdout", f"{i}") for i in range(50)], per_chunk_delay=0.05
    )
    call = asyncio.create_task(
        hub.call_tool("dev-1", "printer", {}, timeout=10, max_output_bytes=100000)
    )
    await asyncio.sleep(0.2)  # let it start streaming
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    # the device received a cancel frame for the exec
    assert any('"type": "cancel"' in raw or '"type":"cancel"' in raw for raw in sconn.sent)
    await sconn.close()
    await asyncio.gather(serve_task, ctask)


def test_summarize_args_redacts_sensitive():
    """Fix D: sensitive args are masked in audit/approval summaries."""
    # built-in hint masking (no schema)
    s = summarize_args({"password": "hunter2", "path": "/a/b"})
    assert s["password"] == "***"
    assert s["path"] == "/a/b"
    # explicit schema-declared sensitive param
    s2 = summarize_args({"license": "ABCDEF"}, sensitive_names=frozenset({"license"}))
    assert s2["license"] == "***"
    # long non-sensitive value truncated
    s3 = summarize_args({"note": "x" * 100})
    assert s3["note"].endswith("…") and len(s3["note"]) <= 41


async def test_audit_log_masks_sensitive_arg(tmp_path):
    """Fix D end-to-end: the persisted exec audit never contains the secret value."""
    hub, serve_task, ctask, _c, sconn = await _online(
        tools=[{"name": "printer", "approval": "auto",
                "params": [{"name": "token", "type": "string", "required": False}]}]
    )
    coord = _coord(hub, tmp_path)
    await coord.call_tool("dev-1", "printer", {"token": "s3cr3t-value"},
                          operator_id="webui", owner_id="webui")
    audit_text = (tmp_path / "connector" / "exec-audit.log").read_text(encoding="utf-8")
    assert "s3cr3t-value" not in audit_text
    assert "***" in audit_text
    await sconn.close()
    await asyncio.gather(serve_task, ctask)
