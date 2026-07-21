"""End-to-end controlled execution: full stack through gateway routes + coordinator.

Exercises the realistic chain a WebUI owner + agent drive together:
list tools → (cross-person) request → grant → call (auto / webui approval) →
observe result → cancel → audit — over the real ConnectorGateway HTTP routes,
ExecutionCoordinator, ConnectorHub, and an in-memory fake device.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from nanobot.connector import protocol as proto
from nanobot.connector.hub import ConnectorError

from .conftest import duplex
from .test_exec_integration import FakeExecConnector
from .test_gateway_exec_http import FakeConnection, FakeRequest, _auth, _body, _gateway


async def _online_device(gw, tools, *, outputs=None, per_chunk_delay=0.0, fingerprint="fp-e2e"):
    code, _ = gw.devices.generate_pairing_code("webui")
    node_id, _tok = gw.devices.redeem_pairing_code(
        code, name="PC", platform="windows", machine_fingerprint=fingerprint
    )
    server_conn, client_conn = duplex()
    serve_task = asyncio.create_task(gw.hub.serve(server_conn, node_id=node_id, owner_id="webui"))
    connector = FakeExecConnector(
        client_conn, tools=tools, outputs=outputs, per_chunk_delay=per_chunk_delay
    )
    ctask = asyncio.create_task(connector.run())
    for _ in range(200):
        if gw.hub.list_nodes():
            break
        await asyncio.sleep(0.01)
    return node_id, server_conn, serve_task, ctask


async def _get(gw, path):
    return await gw.handle_http(FakeConnection(), FakeRequest(path, _auth()), path.split("?")[0])


def _audit_lines(tmp_path):
    audit = tmp_path / "connector" / "exec-audit.log"
    if not audit.exists():
        return []
    return [json.loads(x) for x in audit.read_text(encoding="utf-8").splitlines() if x.strip()]


async def test_e2e_self_use_auto_tool(tmp_path):
    """Owner lists tools over HTTP, then the agent runs an auto tool — audited."""
    gw = _gateway(tmp_path)
    node_id, sconn, serve_task, ctask = await _online_device(
        gw, [{"name": "open_notepad", "approval": "auto", "params": []}],
        outputs=[("stdout", "opened\n")],
    )

    # owner-facing: list tools via the HTTP route
    listed = await _get(gw, f"/api/connector/tools?nodeId={node_id}")
    assert [t["name"] for t in _body(listed)["tools"]] == ["open_notepad"]

    # agent-facing: run it through the coordinator
    result = await gw.coordinator.call_tool(
        node_id, "open_notepad", {}, operator_id="webui", owner_id="webui"
    )
    assert result.exit_code == 0 and result.stdout == "opened\n"

    audit = _audit_lines(tmp_path)
    assert any(r["tool"] == "open_notepad" and r["result"] == "ok" for r in audit)

    await sconn.close()
    await asyncio.gather(serve_task, ctask)


async def test_e2e_webui_approval_full_loop(tmp_path):
    """A webui-policy tool blocks until the owner approves via the HTTP route."""
    gw = _gateway(tmp_path)
    node_id, sconn, serve_task, ctask = await _online_device(
        gw, [{"name": "deploy", "approval": "webui", "params": []}],
        outputs=[("stdout", "deployed\n")],
    )

    call = asyncio.create_task(
        gw.coordinator.call_tool(node_id, "deploy", {}, operator_id="webui", owner_id="webui")
    )

    approval_id = None
    for _ in range(200):
        pending = _body(await _get(gw, "/api/connector/approvals"))["approvals"]
        if pending:
            approval_id = pending[0]["approvalId"]
            break
        await asyncio.sleep(0.02)
    assert approval_id is not None

    approve = await _get(gw, f"/api/connector/approve?approvalId={approval_id}&decision=approve")
    assert _body(approve)["approved"] is True

    result = await call
    assert result.exit_code == 0

    audit = _audit_lines(tmp_path)
    assert any(r["tool"] == "deploy" and r["approval"] == "webui" and r["result"] == "ok" for r in audit)

    await sconn.close()
    await asyncio.gather(serve_task, ctask)


async def test_e2e_cross_person_request_grant_run(tmp_path):
    """Alice can only run after the owner accepts her request over HTTP."""
    gw = _gateway(tmp_path)
    node_id, sconn, serve_task, ctask = await _online_device(
        gw, [{"name": "run_report", "approval": "auto", "params": []}],
        outputs=[("stdout", "ok\n")],
    )

    # before any grant, cross-person call is hidden as not-found
    with pytest.raises(ConnectorError) as ei:
        await gw.coordinator.call_tool(
            node_id, "run_report", {}, operator_id="alice", owner_id="webui"
        )
    assert ei.value.code == proto.ERROR_TOOL_NOT_FOUND

    # alice requests access; owner sees it and grants over HTTP
    gw.authz.request_access(node_id, "alice", tools=["run_report"], reason="monthly report")
    reqs = _body(await _get(gw, "/api/connector/requests"))["requests"]
    assert any(r["operatorId"] == "alice" for r in reqs)

    granted = await _get(
        gw, f"/api/connector/grant?nodeId={node_id}&tool=run_report&operatorId=alice&ttlS=3600"
    )
    assert granted.status_code == 200
    # granting cleared the pending request
    assert _body(await _get(gw, "/api/connector/requests"))["requests"] == []

    # now alice's call runs
    result = await gw.coordinator.call_tool(
        node_id, "run_report", {}, operator_id="alice", owner_id="webui"
    )
    assert result.exit_code == 0

    # owner revokes; alice is denied again
    await _get(gw, f"/api/connector/revoke-grant?nodeId={node_id}&tool=run_report&operatorId=alice")
    with pytest.raises(ConnectorError):
        await gw.coordinator.call_tool(
            node_id, "run_report", {}, operator_id="alice", owner_id="webui"
        )

    await sconn.close()
    await asyncio.gather(serve_task, ctask)


async def test_e2e_cancel_during_run(tmp_path):
    """Cancelling mid-run terminates the device execution and reports cancelled."""
    gw = _gateway(tmp_path)
    node_id, sconn, serve_task, ctask = await _online_device(
        gw, [{"name": "long_job", "approval": "auto", "params": []}],
        outputs=[("stdout", f"{i}") for i in range(50)], per_chunk_delay=0.05,
    )

    cancel = asyncio.Event()

    async def canceller():
        await asyncio.sleep(0.2)
        cancel.set()

    tk = asyncio.create_task(canceller())
    result = await gw.coordinator.call_tool(
        node_id, "long_job", {}, operator_id="webui", owner_id="webui", cancel_event=cancel
    )
    await tk
    assert result.cancelled is True

    await sconn.close()
    await asyncio.gather(serve_task, ctask)


async def test_e2e_metrics_reflect_activity(tmp_path):
    """Exec metrics accumulate and are readable through the HTTP route."""
    gw = _gateway(tmp_path)
    node_id, sconn, serve_task, ctask = await _online_device(
        gw, [{"name": "ping", "approval": "auto", "params": []}], outputs=[("stdout", "pong")],
    )
    await gw.coordinator.call_tool(node_id, "ping", {}, operator_id="webui", owner_id="webui")

    snap = _body(await _get(gw, "/api/connector/exec-metrics"))
    assert snap["executions"] >= 1

    await sconn.close()
    await asyncio.gather(serve_task, ctask)
