"""Tests for the connector wire protocol (task 1.3)."""

import pytest

from nanobot.connector import protocol as proto


def test_register_frame_roundtrip():
    frame = proto.RegisterFrame(
        node=proto.NodeInfo(
            name="Xu 的台式机",
            platform="windows",
            version="1.0.0",
            roots=["D:/PPT资料"],
            fingerprint="abc123",
        )
    )
    raw = proto.dump_frame(frame)
    assert raw["type"] == "register"
    assert raw["protocol"] == proto.PROTOCOL_VERSION
    assert raw["node"]["roots"] == ["D:/PPT资料"]

    parsed = proto.parse_frame(raw)
    assert isinstance(parsed, proto.RegisterFrame)
    assert parsed.node.name == "Xu 的台式机"
    assert parsed.node.fingerprint == "abc123"


def test_registered_frame_camel_alias():
    frame = proto.RegisteredFrame(node_id="dev-a1b2", heartbeat_interval_s=20)
    raw = proto.dump_frame(frame)
    assert raw["nodeId"] == "dev-a1b2"
    assert raw["heartbeatIntervalS"] == 20
    parsed = proto.parse_frame(raw)
    assert isinstance(parsed, proto.RegisteredFrame)
    assert parsed.node_id == "dev-a1b2"


def test_rpc_request_and_response_roundtrip():
    req = proto.RpcRequestFrame(id="7f3a", method="fs.list", params={"path": "D:/x"})
    parsed_req = proto.parse_frame(proto.dump_frame(req))
    assert isinstance(parsed_req, proto.RpcRequestFrame)
    assert parsed_req.method == "fs.list"
    assert parsed_req.params["path"] == "D:/x"

    ok = proto.RpcResponseFrame(id="7f3a", ok=True, result={"entries": []})
    parsed_ok = proto.parse_frame(proto.dump_frame(ok))
    assert isinstance(parsed_ok, proto.RpcResponseFrame)
    assert parsed_ok.ok is True

    err = proto.RpcResponseFrame(
        id="7f3a", ok=False, error={"code": proto.ERROR_PATH_DENIED, "message": "no"}
    )
    parsed_err = proto.parse_frame(proto.dump_frame(err))
    assert parsed_err.ok is False
    assert parsed_err.error["code"] == proto.ERROR_PATH_DENIED


def test_file_chunk_eof_sentinel():
    tail = proto.FileChunkFrame(
        id="7f3a", seq=41, data="", eof=True, sha256="deadbeef", total_bytes=1024
    )
    raw = proto.dump_frame(tail)
    parsed = proto.parse_frame(raw)
    assert isinstance(parsed, proto.FileChunkFrame)
    assert parsed.eof is True
    assert parsed.sha256 == "deadbeef"
    assert parsed.total_bytes == 1024


def test_file_chunk_accepts_client_camelcase_total_bytes():
    """The real client sends ``totalBytes`` (camelCase) — it must populate total_bytes."""
    raw = {
        "type": "file_chunk",
        "id": "7f3a",
        "seq": 3,
        "data": "",
        "eof": True,
        "sha256": "deadbeef",
        "totalBytes": 2048,
    }
    parsed = proto.parse_frame(raw)
    assert isinstance(parsed, proto.FileChunkFrame)
    assert parsed.total_bytes == 2048
    # And dumps back out as camelCase for symmetry.
    assert proto.dump_frame(parsed)["totalBytes"] == 2048


def test_unknown_fields_tolerated():
    raw = {
        "type": "register",
        "protocol": 1,
        "node": {"name": "x", "future_field": "ignored"},
        "another_future_field": 123,
    }
    parsed = proto.parse_frame(raw)
    assert isinstance(parsed, proto.RegisterFrame)
    assert parsed.node.name == "x"


def test_unknown_type_raises():
    with pytest.raises(proto.ProtocolError):
        proto.parse_frame({"type": "does_not_exist"})


def test_non_object_raises():
    with pytest.raises(proto.ProtocolError):
        proto.parse_frame(["not", "a", "dict"])  # type: ignore[arg-type]


def test_cancel_and_revoked_frames():
    assert proto.parse_frame(proto.dump_frame(proto.CancelFrame(id="7f3a"))).id == "7f3a"
    assert isinstance(proto.parse_frame(proto.dump_frame(proto.RevokedFrame())), proto.RevokedFrame)


# --- v2: controlled execution (add-connector-local-tools) ----------------


def test_protocol_version_is_v2():
    assert proto.PROTOCOL_VERSION == 2


def test_rpc_methods_include_fs_and_exec():
    assert proto.FS_METHODS <= proto.RPC_METHODS
    assert proto.EXEC_METHODS <= proto.RPC_METHODS
    assert {"tools.list", "tools.call", "tools.cancel"} == proto.EXEC_METHODS
    assert proto.FS_METHODS.isdisjoint(proto.EXEC_METHODS)


def test_node_capabilities_default_to_fs_only():
    """A v1 client omits capabilities; it must degrade to filesystem-only."""
    raw = {"type": "register", "protocol": 1, "node": {"name": "old-client"}}
    parsed = proto.parse_frame(raw)
    assert isinstance(parsed, proto.RegisterFrame)
    assert parsed.node.capabilities == [proto.CAP_FS]
    assert proto.CAP_EXEC not in parsed.node.capabilities


def test_node_capabilities_roundtrip_with_exec():
    frame = proto.RegisterFrame(
        node=proto.NodeInfo(
            name="v2-client",
            platform="windows",
            capabilities=[proto.CAP_FS, proto.CAP_EXEC],
        )
    )
    raw = proto.dump_frame(frame)
    assert raw["node"]["capabilities"] == ["fs", "exec"]
    parsed = proto.parse_frame(raw)
    assert proto.CAP_EXEC in parsed.node.capabilities


def test_exec_output_frame_roundtrip():
    frame = proto.ExecOutputFrame(id="ex1", stream="stderr", seq=3, data="line\n")
    raw = proto.dump_frame(frame)
    assert raw["type"] == "exec_output"
    assert raw["stream"] == "stderr"
    parsed = proto.parse_frame(raw)
    assert isinstance(parsed, proto.ExecOutputFrame)
    assert parsed.seq == 3
    assert parsed.data == "line\n"


def test_exec_result_frame_camel_aliases():
    frame = proto.ExecResultFrame(
        id="ex1", exit_code=0, duration_ms=1234, timed_out=False, truncated=True
    )
    raw = proto.dump_frame(frame)
    assert raw["exitCode"] == 0
    assert raw["durationMs"] == 1234
    assert raw["truncated"] is True
    parsed = proto.parse_frame(raw)
    assert isinstance(parsed, proto.ExecResultFrame)
    assert parsed.exit_code == 0
    assert parsed.duration_ms == 1234
    assert parsed.truncated is True


def test_exec_result_accepts_client_camelcase():
    raw = {
        "type": "exec_result",
        "id": "ex1",
        "exitCode": 137,
        "durationMs": 50,
        "timedOut": True,
        "cancelled": True,
    }
    parsed = proto.parse_frame(raw)
    assert isinstance(parsed, proto.ExecResultFrame)
    assert parsed.exit_code == 137
    assert parsed.timed_out is True
    assert parsed.cancelled is True


def test_exec_error_codes_are_distinct():
    codes = {
        proto.ERROR_EXEC_UNSUPPORTED,
        proto.ERROR_TOOL_NOT_FOUND,
        proto.ERROR_INVALID_ARGS,
        proto.ERROR_MISSING_CREDENTIAL,
        proto.ERROR_EXEC_DENIED,
        proto.ERROR_APPROVAL_DENIED,
        proto.ERROR_APPROVAL_TIMEOUT,
        proto.ERROR_EXEC_LIMIT,
        proto.ERROR_EXEC_TIMEOUT,
        proto.ERROR_EXEC_CANCELLED,
    }
    assert len(codes) == 10  # all unique


# --- v2.5: local MCP proxy (add-connector-mcp-proxy) ---------------------


def test_mcp_methods_in_rpc_set():
    assert proto.MCP_METHODS == {"mcp.list", "mcp.call"}
    assert proto.MCP_METHODS <= proto.RPC_METHODS
    # mcp methods are disjoint from fs and exec
    assert proto.MCP_METHODS.isdisjoint(proto.FS_METHODS)
    assert proto.MCP_METHODS.isdisjoint(proto.EXEC_METHODS)


def test_mcp_capability_token():
    assert proto.CAP_MCP == "mcp"
    frame = proto.RegisterFrame(
        node=proto.NodeInfo(name="n", capabilities=[proto.CAP_FS, proto.CAP_EXEC, proto.CAP_MCP])
    )
    parsed = proto.parse_frame(proto.dump_frame(frame))
    assert proto.CAP_MCP in parsed.node.capabilities


def test_mcp_error_codes_distinct():
    assert proto.ERROR_MCP_UNSUPPORTED != proto.ERROR_MCP_UNAVAILABLE
    assert proto.ERROR_MCP_UNSUPPORTED not in {proto.ERROR_EXEC_UNSUPPORTED}


# --- v3: desktop control (add-connector-desktop-control) -----------------


def test_desktop_methods_in_rpc_set():
    assert proto.DESKTOP_METHODS == {
        "desktop.session.start", "desktop.session.end", "desktop.capture", "desktop.input",
    }
    assert proto.DESKTOP_METHODS <= proto.RPC_METHODS
    assert proto.DESKTOP_METHODS.isdisjoint(proto.FS_METHODS)
    assert proto.DESKTOP_METHODS.isdisjoint(proto.EXEC_METHODS)
    assert proto.DESKTOP_METHODS.isdisjoint(proto.MCP_METHODS)


def test_desktop_capability_token():
    assert proto.CAP_DESKTOP == "desktop"
    frame = proto.RegisterFrame(
        node=proto.NodeInfo(name="n", capabilities=[proto.CAP_FS, proto.CAP_DESKTOP])
    )
    parsed = proto.parse_frame(proto.dump_frame(frame))
    assert proto.CAP_DESKTOP in parsed.node.capabilities


def test_desktop_action_whitelist():
    assert "click" in proto.DESKTOP_ACTIONS
    assert "type" in proto.DESKTOP_ACTIONS
    assert "format_disk" not in proto.DESKTOP_ACTIONS


def test_desktop_error_codes_distinct():
    codes = {
        proto.ERROR_DESKTOP_UNSUPPORTED,
        proto.ERROR_SESSION_INACTIVE,
        proto.ERROR_SESSION_ENDED,
        proto.ERROR_OUT_OF_BOUNDS,
        proto.ERROR_NO_PERMISSION,
        proto.ERROR_SENSITIVE_UNCONFIRMED,
    }
    assert len(codes) == 6
