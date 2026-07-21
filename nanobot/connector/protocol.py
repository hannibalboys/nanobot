"""Wire protocol for the nanobot Connector data channel.

All frames are JSON text frames over a single WebSocket connection. Every frame
carries a ``type`` discriminator. File contents ride inline as base64 in
``file_chunk`` frames. The protocol is versioned via :data:`PROTOCOL_VERSION`;
the ``register`` frame declares the client's version so the server can refuse an
incompatible peer instead of failing in surprising ways later.

Models are intentionally permissive about unknown fields (``extra="allow"``) so a
newer peer can add fields without breaking an older one — forward compatibility
that lets a v2 peer talk to a v1 peer (and vice versa) without a hard break.

Protocol v2 (add-connector-local-tools) adds *controlled local execution*: the
``tools.list`` / ``tools.call`` / ``tools.cancel`` methods, the ``exec_output``
(streamed stdout/stderr) and ``exec_result`` (terminal status) frames, and a
``capabilities`` list on the register frame so the server can gate exec on nodes
that actually support it. Pre-execution failures (unknown tool, bad args, denied)
still come back as ``rpc_response(ok=false)`` — mirroring ``fs.fetch`` — while a
started execution streams ``exec_output`` and ends with ``exec_result``.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

PROTOCOL_VERSION = 2

# Read-only filesystem methods (v1 baseline, always available).
FS_METHODS = frozenset({"fs.list", "fs.search", "fs.read", "fs.stat", "fs.fetch"})
# Controlled-execution methods (v2, gated on the ``exec`` capability).
EXEC_METHODS = frozenset({"tools.list", "tools.call", "tools.cancel"})
# Local MCP proxy methods (v2.5, gated on the ``mcp`` capability). Plain
# request/response — a bridged MCP tool call is not a long-running stream.
MCP_METHODS = frozenset({"mcp.list", "mcp.call"})
# Desktop control methods (v3, gated on the ``desktop`` capability). Screen
# capture + input injection, only inside an authorized controlled session.
DESKTOP_METHODS = frozenset(
    {"desktop.session.start", "desktop.session.end", "desktop.capture", "desktop.input"}
)
# RPC methods the server may invoke on the connector.
RPC_METHODS = FS_METHODS | EXEC_METHODS | MCP_METHODS | DESKTOP_METHODS

# Node capability tokens declared in the register frame.
CAP_FS = "fs"
CAP_EXEC = "exec"
CAP_MCP = "mcp"
CAP_DESKTOP = "desktop"

# Desktop input action types the connector may inject (whitelist).
DESKTOP_ACTIONS = frozenset(
    {"click", "double_click", "right_click", "type", "key", "scroll", "drag", "move", "wait"}
)

# Stable error codes shared across gateway, tools, and client.
ERROR_PATH_DENIED = "path_denied"
ERROR_NOT_FOUND = "not_found"
ERROR_TOO_LARGE = "too_large"
ERROR_NODE_OFFLINE = "node_offline"
ERROR_RPC_TIMEOUT = "rpc_timeout"
ERROR_PROTOCOL_UNSUPPORTED = "protocol_unsupported"
ERROR_DECODE = "decode"
ERROR_NOT_TEXT = "not_text"
ERROR_INTERNAL = "internal"

# v2 controlled-execution error codes (add-connector-local-tools). These are
# shared verbatim across protocol, gateway, and agent tools so a failure keeps
# the same code at every layer.
ERROR_EXEC_UNSUPPORTED = "exec_unsupported"  # device has no ``exec`` capability
ERROR_TOOL_NOT_FOUND = "tool_not_found"  # tool not registered in tools.json
ERROR_INVALID_ARGS = "invalid_args"  # args failed template validation
ERROR_MISSING_CREDENTIAL = "missing_credential"  # declared credential not set on device
ERROR_EXEC_DENIED = "exec_denied"  # operator not authorized for this tool
ERROR_APPROVAL_DENIED = "approval_denied"  # approver rejected the execution
ERROR_APPROVAL_TIMEOUT = "approval_timeout"  # approval TTL elapsed (default-deny)
ERROR_EXEC_LIMIT = "exec_limit"  # concurrency or rate limit hit
ERROR_EXEC_TIMEOUT = "exec_timeout"  # execution exceeded its timeout
ERROR_EXEC_CANCELLED = "exec_cancelled"  # cancelled by caller or device owner

# v2.5 local MCP proxy error codes (add-connector-mcp-proxy).
ERROR_MCP_UNSUPPORTED = "mcp_unsupported"  # device has no ``mcp`` capability
ERROR_MCP_UNAVAILABLE = "mcp_unavailable"  # bridged server not ready / disconnected

# v3 desktop control error codes (add-connector-desktop-control).
ERROR_DESKTOP_UNSUPPORTED = "desktop_unsupported"  # device has no ``desktop`` capability
ERROR_SESSION_INACTIVE = "session_inactive"  # capture/input with no active session
ERROR_SESSION_ENDED = "session_ended"  # session ended: timeout / taken over / terminated
ERROR_OUT_OF_BOUNDS = "out_of_bounds"  # input coordinate outside the screen
ERROR_NO_PERMISSION = "no_permission"  # OS screen-recording / accessibility not granted
ERROR_SENSITIVE_UNCONFIRMED = "sensitive_unconfirmed"  # sensitive action not confirmed


class _Frame(BaseModel):
    """Base for all protocol frames: camel/snake tolerant, forward-compatible."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")


# --- connector -> server -------------------------------------------------


class NodeInfo(_Frame):
    """Node self-description sent inside a ``register`` frame.

    ``capabilities`` lets a node advertise what it supports so the server can
    gate features (e.g. only route ``tools.*`` to a node that lists ``exec``).
    A v1 connector omits the field; it then defaults to filesystem-only, so an
    old client transparently degrades to read-only on a v2 server.
    """

    name: str = "unknown"
    platform: str = "unknown"
    version: str = "0.0.0"
    roots: list[str] = Field(default_factory=list)
    fingerprint: str = ""
    capabilities: list[str] = Field(default_factory=lambda: [CAP_FS])


class RegisterFrame(_Frame):
    type: Literal["register"] = "register"
    protocol: int = PROTOCOL_VERSION
    node: NodeInfo = Field(default_factory=NodeInfo)


class HeartbeatFrame(_Frame):
    type: Literal["heartbeat"] = "heartbeat"
    ts: int = 0


class RpcResponseFrame(_Frame):
    type: Literal["rpc_response"] = "rpc_response"
    id: str
    ok: bool = True
    result: Any = None
    error: dict[str, Any] | None = None


class FileChunkFrame(_Frame):
    type: Literal["file_chunk"] = "file_chunk"
    id: str
    seq: int = 0
    data: str = ""  # base64; empty on the eof sentinel
    eof: bool = False
    sha256: str | None = None
    total_bytes: int | None = Field(default=None, alias="totalBytes")


class ExecOutputFrame(_Frame):
    """Incremental stdout/stderr from a running ``tools.call`` execution.

    ``data`` is UTF-8 text (already decoded on the device, lossily if needed) so
    the server can stream it straight to the caller. ``stream`` is ``stdout`` or
    ``stderr``; ``seq`` orders frames within one stream.
    """

    type: Literal["exec_output"] = "exec_output"
    id: str
    stream: Literal["stdout", "stderr"] = "stdout"
    seq: int = 0
    data: str = ""


class ExecResultFrame(_Frame):
    """Terminal status of a ``tools.call`` execution (the exec analogue of eof).

    A started execution always ends with exactly one of these. Pre-start refusals
    (unknown tool, invalid args, denied) come back as ``rpc_response(ok=false)``
    instead, so the caller can tell "never ran" from "ran and exited".
    """

    type: Literal["exec_result"] = "exec_result"
    id: str
    exit_code: int | None = Field(default=None, alias="exitCode")
    duration_ms: int = Field(default=0, alias="durationMs")
    timed_out: bool = Field(default=False, alias="timedOut")
    truncated: bool = False
    cancelled: bool = False


# --- server -> connector -------------------------------------------------


class RegisteredFrame(_Frame):
    type: Literal["registered"] = "registered"
    node_id: str = Field(alias="nodeId")
    heartbeat_interval_s: int = Field(default=20, alias="heartbeatIntervalS")


class RpcRequestFrame(_Frame):
    type: Literal["rpc_request"] = "rpc_request"
    id: str
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


class CancelFrame(_Frame):
    type: Literal["cancel"] = "cancel"
    id: str


class RevokedFrame(_Frame):
    type: Literal["revoked"] = "revoked"


class ErrorFrame(_Frame):
    type: Literal["error"] = "error"
    code: str = ERROR_INTERNAL
    message: str = ""


_FRAME_TYPES: dict[str, type[_Frame]] = {
    "register": RegisterFrame,
    "heartbeat": HeartbeatFrame,
    "rpc_response": RpcResponseFrame,
    "file_chunk": FileChunkFrame,
    "exec_output": ExecOutputFrame,
    "exec_result": ExecResultFrame,
    "registered": RegisteredFrame,
    "rpc_request": RpcRequestFrame,
    "cancel": CancelFrame,
    "revoked": RevokedFrame,
    "error": ErrorFrame,
}


class ProtocolError(ValueError):
    """Raised when a frame cannot be decoded into a known type."""


def parse_frame(raw: dict[str, Any]) -> _Frame:
    """Decode a JSON object into the matching frame model.

    Unknown ``type`` values raise :class:`ProtocolError`; unknown *fields* are
    tolerated so a newer peer stays compatible with an older one.
    """
    if not isinstance(raw, dict):
        raise ProtocolError(f"frame must be an object, got {type(raw).__name__}")
    frame_type = raw.get("type")
    model = _FRAME_TYPES.get(frame_type) if isinstance(frame_type, str) else None
    if model is None:
        raise ProtocolError(f"unknown frame type: {frame_type!r}")
    return model.model_validate(raw)


def dump_frame(frame: _Frame) -> dict[str, Any]:
    """Serialize a frame to a JSON-ready dict using camelCase aliases."""
    return frame.model_dump(by_alias=True, exclude_none=True)
