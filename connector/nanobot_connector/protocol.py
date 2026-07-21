"""Wire protocol constants for the connector client.

Standalone mirror of the server's ``nanobot.connector.protocol`` so the client
package has no dependency on the server package. Frames are plain dicts here —
the client only needs to build and read a handful of JSON messages.

Kept in lockstep with ``nanobot/connector/protocol.py``: v2 adds controlled
execution (``tools.*`` methods, ``exec_output`` / ``exec_result`` frames, node
``capabilities``) and the exec error codes.
"""

from __future__ import annotations

PROTOCOL_VERSION = 2

# Node capability tokens.
CAP_FS = "fs"
CAP_EXEC = "exec"
CAP_MCP = "mcp"
CAP_DESKTOP = "desktop"

# Desktop input action whitelist.
DESKTOP_ACTIONS = frozenset(
    {"click", "double_click", "right_click", "type", "key", "scroll", "drag", "move", "wait"}
)

ERROR_PATH_DENIED = "path_denied"
ERROR_NOT_FOUND = "not_found"
ERROR_TOO_LARGE = "too_large"
ERROR_NOT_TEXT = "not_text"
ERROR_DECODE = "decode"
ERROR_INTERNAL = "internal"

# v2 controlled-execution error codes.
ERROR_EXEC_UNSUPPORTED = "exec_unsupported"
ERROR_TOOL_NOT_FOUND = "tool_not_found"
ERROR_INVALID_ARGS = "invalid_args"
ERROR_MISSING_CREDENTIAL = "missing_credential"
ERROR_EXEC_DENIED = "exec_denied"
ERROR_APPROVAL_DENIED = "approval_denied"
ERROR_APPROVAL_TIMEOUT = "approval_timeout"
ERROR_EXEC_LIMIT = "exec_limit"
ERROR_EXEC_TIMEOUT = "exec_timeout"
ERROR_EXEC_CANCELLED = "exec_cancelled"

# v2.5 local MCP proxy error codes.
ERROR_MCP_UNSUPPORTED = "mcp_unsupported"
ERROR_MCP_UNAVAILABLE = "mcp_unavailable"

# v3 desktop control error codes.
ERROR_DESKTOP_UNSUPPORTED = "desktop_unsupported"
ERROR_SESSION_INACTIVE = "session_inactive"
ERROR_SESSION_ENDED = "session_ended"
ERROR_OUT_OF_BOUNDS = "out_of_bounds"
ERROR_NO_PERMISSION = "no_permission"
ERROR_SENSITIVE_UNCONFIRMED = "sensitive_unconfirmed"


def register_frame(node: dict, protocol: int = PROTOCOL_VERSION) -> dict:
    return {"type": "register", "protocol": protocol, "node": node}


def heartbeat_frame(ts: int) -> dict:
    return {"type": "heartbeat", "ts": ts}


def rpc_response(rpc_id: str, result: object) -> dict:
    return {"type": "rpc_response", "id": rpc_id, "ok": True, "result": result}


def rpc_error(rpc_id: str, code: str, message: str) -> dict:
    return {
        "type": "rpc_response",
        "id": rpc_id,
        "ok": False,
        "error": {"code": code, "message": message},
    }


def file_chunk(rpc_id: str, seq: int, data_b64: str, total_bytes: int | None = None) -> dict:
    frame = {"type": "file_chunk", "id": rpc_id, "seq": seq, "data": data_b64, "eof": False}
    if total_bytes is not None:
        frame["totalBytes"] = total_bytes
    return frame


def file_chunk_eof(rpc_id: str, seq: int, sha256: str, total_bytes: int) -> dict:
    return {
        "type": "file_chunk",
        "id": rpc_id,
        "seq": seq,
        "data": "",
        "eof": True,
        "sha256": sha256,
        "totalBytes": total_bytes,
    }


def exec_output(rpc_id: str, stream: str, seq: int, data: str) -> dict:
    return {"type": "exec_output", "id": rpc_id, "stream": stream, "seq": seq, "data": data}


def exec_result(
    rpc_id: str,
    *,
    exit_code: int | None,
    duration_ms: int,
    timed_out: bool = False,
    truncated: bool = False,
    cancelled: bool = False,
) -> dict:
    return {
        "type": "exec_result",
        "id": rpc_id,
        "exitCode": exit_code,
        "durationMs": duration_ms,
        "timedOut": timed_out,
        "truncated": truncated,
        "cancelled": cancelled,
    }
