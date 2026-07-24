"""Agent tools for the nanobot Connector (local file connector).

These expose a read-only, allow-listed slice of a paired device's filesystem to
the agent. They talk to the process-global :class:`ConnectorHub`; the connector
daemon enforces its own directory allow-list, and the gateway enforces landing
containment — these tools are the LLM-facing surface plus error translation.

Registration is gated on ``connector.enabled`` (task 4.1). Errors from the hub
are translated into actionable :class:`ToolResult` messages (task 4.2), and
device visibility is scoped by owner (task 4.3).
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.context import RequestContext, current_request_context
from nanobot.connector import protocol as proto
from nanobot.connector.audit import audit_file_access
from nanobot.connector.hub import ConnectorError, default_hub
from nanobot.runtime_context import RuntimeContextBlock

_DEFAULT_OWNER = "webui"

# Actionable guidance per error code, appended to the raw message.
_ERROR_HINTS = {
    proto.ERROR_NODE_OFFLINE: (
        "The device is offline. Ask the user to make sure the nanobot connector "
        "app is running and connected on that computer."
    ),
    proto.ERROR_RPC_TIMEOUT: "The device did not respond in time; you may retry once.",
    proto.ERROR_PATH_DENIED: (
        "That path is outside the device's shared folders. Ask the user to add it "
        "with `nanobot-connector allow <folder>`."
    ),
    proto.ERROR_TOO_LARGE: (
        "The file is too large to transfer. Ask the user for a smaller file or a summary."
    ),
    proto.ERROR_NOT_FOUND: "No such file or folder on the device; try a different path.",
    proto.ERROR_NOT_TEXT: (
        "The file is not UTF-8 text. Use connector_fetch_file to download it, then "
        "process it with server-side tools."
    ),
    proto.ERROR_EXEC_UNSUPPORTED: (
        "This device does not support running tools (it's an older connector or has "
        "execution disabled). Ask the user to update the connector app."
    ),
    proto.ERROR_TOOL_NOT_FOUND: (
        "That tool is not available on this device. Use connector_list_tools to see "
        "which tools the device owner has registered."
    ),
    proto.ERROR_INVALID_ARGS: "The arguments are invalid for this tool; check the tool's parameters.",
    proto.ERROR_MISSING_CREDENTIAL: (
        "The tool needs a credential that isn't configured on the device. Ask the "
        "user to set it locally with `nanobot-connector tool secret set`."
    ),
    proto.ERROR_EXEC_DENIED: (
        "You are not authorized to run this tool on that device. Ask the device owner "
        "to grant access."
    ),
    proto.ERROR_APPROVAL_DENIED: (
        "The execution was not approved by the device owner. For approval=local "
        "tools this means the owner's arm window is closed — ask them to arm the "
        "capability on their machine (e.g. `nanobot-connector arm exec --for 30m`), "
        "then retry."
    ),
    proto.ERROR_APPROVAL_TIMEOUT: (
        "The approval request timed out and was declined. You may try again."
    ),
    proto.ERROR_EXEC_LIMIT: (
        "The device is busy or the rate limit was hit; wait a moment and retry."
    ),
    proto.ERROR_EXEC_TIMEOUT: "The tool ran too long and was stopped.",
    proto.ERROR_EXEC_CANCELLED: "The execution was cancelled.",
    proto.ERROR_MCP_UNSUPPORTED: (
        "This device does not bridge any local MCP servers, or MCP proxy is disabled."
    ),
    proto.ERROR_MCP_UNAVAILABLE: (
        "The local MCP server is not reachable right now; ask the user to check it, "
        "then retry."
    ),
    proto.ERROR_DESKTOP_UNSUPPORTED: (
        "This device does not support desktop control, or it is not enabled/authorized."
    ),
    proto.ERROR_SESSION_INACTIVE: "There is no active desktop session; start one first.",
    proto.ERROR_SESSION_ENDED: (
        "The desktop session ended (timed out, was taken over, or terminated). Start a new one."
    ),
    proto.ERROR_OUT_OF_BOUNDS: "That action's coordinates are outside the screen; re-check the screenshot.",
    proto.ERROR_NO_PERMISSION: (
        "The device lacks OS screen-recording/accessibility permission. Ask the user to grant it."
    ),
    proto.ERROR_SENSITIVE_UNCONFIRMED: (
        "That action was flagged sensitive and the device owner did not confirm it."
    ),
}


class _ConnectorTool(Tool):
    """Shared base: config gating, hub access, owner scoping, error mapping."""

    config_key = "connector"
    read_only = True

    def __init__(
        self,
        *,
        connector_config: Any,
        workspace: Path,
        hub: Any | None = None,
        owner_id: str = _DEFAULT_OWNER,
    ) -> None:
        self._config = connector_config
        self._workspace = Path(workspace)
        self._hub = hub if hub is not None else default_hub()
        self._owner_id = owner_id

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        cfg = getattr(ctx, "connector_config", None)
        return bool(cfg is not None and cfg.enabled)

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(
            connector_config=ctx.connector_config,
            workspace=Path(ctx.workspace),
        )

    # -- helpers ------------------------------------------------------------

    def _session(self) -> str | None:
        rc = current_request_context()
        return rc.session_key if rc else None

    def _map_error(self, exc: ConnectorError) -> ToolResult:
        hint = _ERROR_HINTS.get(exc.code, "")
        message = f"connector error [{exc.code}]: {exc.message}"
        return ToolResult.error(f"{message} {hint}".strip())

    async def _rpc(self, node_id: str, method: str, params: dict[str, Any]) -> Any:
        return await self._hub.rpc(
            node_id, method, params,
            timeout=self._config.rpc_timeout_s,
            owner_id=self._owner_id,
        )


@tool_parameters({"type": "object", "properties": {}, "additionalProperties": False})
class ConnectorListNodesTool(_ConnectorTool):
    name = "connector_list_nodes"
    description = (
        "List the user's online local devices (computers running the nanobot "
        "connector). Returns each device's node_id, name, platform, and shared "
        "root folders. The response also includes the gateway's effective capabilities; "
        "do not infer server configuration from a device's reported capabilities or "
        "from earlier conversation history. Use this first to discover which device to "
        "read files from."
    )

    def runtime_context_provider(self):
        return self._provide_runtime_context

    def _effective_capabilities(self) -> dict[str, bool]:
        return {
            "fs": True,
            "exec": bool(getattr(self._config, "allow_exec", False)),
            "mcp": bool(getattr(self._config, "allow_mcp_proxy", False)),
            "desktop": bool(getattr(self._config, "allow_desktop_control", False)),
        }

    async def _provide_runtime_context(
        self,
        _request: RequestContext,
    ) -> RuntimeContextBlock:
        capabilities = self._effective_capabilities()
        enabled = ", ".join(name for name, value in capabilities.items() if value)
        disabled = ", ".join(name for name, value in capabilities.items() if not value)
        return RuntimeContextBlock(
            source="connector_capabilities",
            content=(
                "连接器事实状态：本轮网关已启用能力为 "
                f"{enabled or '无'}；未启用能力为 {disabled or '无'}。"
                "设备上报的 capabilities 只表示客户端支持，不能据此否定网关开关，"
                "也不得用早先对话记录与此事实状态矛盾。"
            ),
        )

    async def execute(self, **kwargs: Any) -> Any:
        nodes = self._hub.list_nodes(owner_id=self._owner_id)
        if not nodes:
            return ToolResult(
                "No local devices are currently online. Ask the user to start the "
                "nanobot connector app on their computer."
            )
        aliases = self._device_aliases()
        for node in nodes:
            alias = aliases.get(node.get("nodeId", ""))
            if alias:
                node["alias"] = alias
        return json.dumps(
            {
                "nodes": nodes,
                "effectiveCapabilities": self._effective_capabilities(),
            },
            ensure_ascii=False,
        )

    def _device_aliases(self) -> dict[str, str]:
        """Best-effort node_id -> owner-set alias, to help the LLM target devices."""
        try:
            from nanobot.connector.devices import DeviceStore

            store = DeviceStore(self._workspace / "connector" / "devices.json")
            return {
                d.node_id: d.alias
                for d in store.list_devices(owner_id=self._owner_id)
                if d.alias
            }
        except Exception:  # noqa: BLE001 - alias is a nicety, never fail listing
            return {}


@tool_parameters({
    "type": "object",
    "properties": {
        "node_id": {"type": "string", "description": "Target device id from connector_list_nodes."},
        "path": {"type": "string", "description": "Directory path on the device (must be inside a shared root)."},
        "max_entries": {"type": "integer", "description": "Max entries to return (default 500).", "minimum": 1, "maximum": 5000},
    },
    "required": ["node_id", "path"],
    "additionalProperties": False,
})
class ConnectorListFilesTool(_ConnectorTool):
    name = "connector_list_files"
    description = "List files and folders in a directory on a paired local device (allow-listed, read-only)."

    async def execute(self, node_id: str, path: str, max_entries: int = 500, **kwargs: Any) -> Any:
        try:
            result = await self._rpc(node_id, "fs.list", {"path": path, "max_entries": max_entries})
        except ConnectorError as exc:
            return self._map_error(exc)
        return json.dumps(result, ensure_ascii=False)


@tool_parameters({
    "type": "object",
    "properties": {
        "node_id": {"type": "string", "description": "Target device id from connector_list_nodes."},
        "query": {"type": "string", "description": "Filename substring to search for."},
        "path": {"type": "string", "description": "Optional subdirectory to scope the search."},
    },
    "required": ["node_id", "query"],
    "additionalProperties": False,
})
class ConnectorSearchFilesTool(_ConnectorTool):
    name = "connector_search_files"
    description = "Search for files by name on a paired local device (allow-listed, read-only)."

    async def execute(self, node_id: str, query: str, path: str = "", **kwargs: Any) -> Any:
        try:
            result = await self._rpc(node_id, "fs.search", {"query": query, "path": path})
        except ConnectorError as exc:
            return self._map_error(exc)
        return json.dumps(result, ensure_ascii=False)


@tool_parameters({
    "type": "object",
    "properties": {
        "node_id": {"type": "string", "description": "Target device id from connector_list_nodes."},
        "path": {"type": "string", "description": "File path on the device (must be inside a shared root)."},
    },
    "required": ["node_id", "path"],
    "additionalProperties": False,
})
class ConnectorReadFileTool(_ConnectorTool):
    name = "connector_read_file"
    description = (
        "Read a small UTF-8 text file from a paired local device and return its "
        "contents inline. For large or binary files use connector_fetch_file."
    )

    async def execute(self, node_id: str, path: str, **kwargs: Any) -> Any:
        try:
            result = await self._rpc(
                node_id, "fs.read",
                {"path": path, "max_bytes": self._config.max_inline_read_bytes},
            )
        except ConnectorError as exc:
            audit_file_access(
                self._workspace, session=self._session(), node_id=node_id,
                method="fs.read", path=path, result=exc.code,
            )
            return self._map_error(exc)
        content = result.get("content") if isinstance(result, dict) else result
        audit_file_access(
            self._workspace, session=self._session(), node_id=node_id,
            method="fs.read", path=path,
            bytes_count=len(content or "") if isinstance(content, str) else 0,
        )
        return content if isinstance(content, str) else json.dumps(result, ensure_ascii=False)


@tool_parameters({
    "type": "object",
    "properties": {
        "node_id": {"type": "string", "description": "Target device id from connector_list_nodes."},
        "path": {"type": "string", "description": "File path on the device (must be inside a shared root)."},
    },
    "required": ["node_id", "path"],
    "additionalProperties": False,
})
class ConnectorFetchFileTool(_ConnectorTool):
    name = "connector_fetch_file"
    description = (
        "Download a file from a paired local device into the server workspace and "
        "return its local server path. Use this to bring the user's local files "
        "onto the server so other tools (read_file, exec) can process them."
    )

    async def execute(self, node_id: str, path: str, **kwargs: Any) -> Any:
        landing_dir = self._workspace / "connector"
        try:
            dest = await self._hub.fetch_file(
                node_id, path,
                base_dir=landing_dir,
                max_file_bytes=self._config.max_file_bytes,
                fetch_cache_max_bytes=self._config.fetch_cache_max_bytes,
                timeout=self._config.transfer_timeout_s,
                owner_id=self._owner_id,
                max_concurrent_transfers=self._config.max_concurrent_transfers,
            )
        except ConnectorError as exc:
            audit_file_access(
                self._workspace, session=self._session(), node_id=node_id,
                method="fs.fetch", path=path, result=exc.code,
            )
            return self._map_error(exc)
        size = dest.stat().st_size if dest.exists() else 0
        audit_file_access(
            self._workspace, session=self._session(), node_id=node_id,
            method="fs.fetch", path=path, bytes_count=size,
        )
        return json.dumps(
            {"server_path": str(dest), "bytes": size}, ensure_ascii=False
        )


# --- controlled execution (add-connector-local-tools) --------------------


def _local_approval_state(tool: dict, *, category: str) -> dict:
    """Derived live-consent field for an ``approval=local`` tool (empty otherwise).

    The device stamps ``armedRemainingS`` (seconds left on the owner's arm
    window) onto local-approval tools in tools.list/mcp.list; older connectors
    omit it. Surfacing a ready-made state string keeps the LLM from having to
    interpret raw seconds — and from claiming a tool "needs approval" when the
    owner has in fact already armed it.
    """
    if tool.get("approval") != "local":
        return {}
    remaining = tool.get("armedRemainingS")
    # bool is a subclass of int; exclude it and any non-numeric wire junk so a
    # malformed device response can never crash the listing.
    if (
        not isinstance(remaining, (int, float))
        or isinstance(remaining, bool)
        or not math.isfinite(remaining)
    ):
        state = "unknown (device connector did not provide a valid live arm status)"
    elif remaining > 0:
        minutes = math.ceil(remaining / 60)
        state = f"armed ({minutes}m remaining) — calls will pass the on-device consent check"
    else:
        state = (
            "not armed — calls will be refused until the device owner arms this "
            f"capability on their machine (e.g. `nanobot-connector arm {category} --for 30m`)"
        )
    return {"localApprovalState": state}


def _completion_safety(tool: dict) -> dict:
    """Give the LLM an explicit guardrail for persistent GUI applications."""
    if tool.get("completion", "wait") != "wait":
        return {}
    return {
        "completionSafety": (
            "wait blocks this call until the local process exits. Do not use it for persistent "
            "GUI applications such as QQ or a browser; ask the owner to set completion=launch first."
        )
    }


class _ConnectorExecTool(_ConnectorTool):
    """Base for exec tools: gated on both ``enabled`` and ``allowExec``, and
    routed through the shared :class:`ExecutionCoordinator` (grants, approvals,
    rate limits, metrics, audit)."""

    read_only = False

    def __init__(self, *, connector_config: Any, workspace: Path, coordinator: Any = None,
                 hub: Any | None = None, owner_id: str = _DEFAULT_OWNER) -> None:
        super().__init__(connector_config=connector_config, workspace=workspace,
                         hub=hub, owner_id=owner_id)
        self._explicit_coordinator = coordinator

    @property
    def _coordinator(self) -> Any:
        """Resolve the shared coordinator lazily on every call.

        The agent builds its tools (and runs this ``__init__``) *before* the
        websocket channel builds the connector gateway, so reading
        ``default_execution_coordinator()`` once at construction time would
        permanently capture ``None``. Resolving per-call picks up the gateway's
        registration whenever it happens.
        """
        if self._explicit_coordinator is not None:
            return self._explicit_coordinator
        from nanobot.connector.exec import default_execution_coordinator

        return default_execution_coordinator()

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        cfg = getattr(ctx, "connector_config", None)
        return bool(cfg is not None and cfg.enabled and getattr(cfg, "allow_exec", False))

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(connector_config=ctx.connector_config, workspace=Path(ctx.workspace))

    def _operator_id(self) -> str:
        # Single-owner deployments: the operator is the device owner (self-use).
        # Multi-user auth would derive the operator from the session here; the
        # coordinator/authz already enforce cross-person grants when they differ.
        return self._owner_id

    def _no_coordinator(self) -> ToolResult:
        # Being here means the tool was *enabled* (config allow_exec=True) yet no
        # shared coordinator was registered — the connector gateway either failed
        # to build or ran before this process. Surface both facts so the failure
        # is diagnosable instead of a misleading "allowExec is off".
        from loguru import logger

        cfg_on = bool(
            self._config is not None
            and getattr(self._config, "allow_exec", False)
        )
        logger.error(
            "connector exec tool has no coordinator: config.allow_exec={} but the "
            "shared ExecutionCoordinator was never registered (gateway not up?)",
            cfg_on,
        )
        return ToolResult.error(
            "Controlled execution coordinator is not running in this process "
            f"(config.allowExec={cfg_on}, shared coordinator missing). "
            "Restart the gateway so the connector service registers it."
        )


@tool_parameters({
    "type": "object",
    "properties": {
        "node_id": {"type": "string", "description": "Target device id from connector_list_nodes."},
    },
    "required": ["node_id"],
    "additionalProperties": False,
})
class ConnectorListToolsTool(_ConnectorExecTool):
    name = "connector_list_tools"
    description = (
        "List the tools the owner of a paired local device has registered for "
        "remote execution. Returns each tool's name, description, parameters, and "
        "approval/completion policy. Call this before connector_call_tool. A local "
        "approval is a device-side pre-authorization check, not a pending WebUI "
        "approval dialog; do not tell the user it is waiting for a click unless the "
        "tool explicitly uses webui approval."
    )

    async def execute(self, node_id: str, **kwargs: Any) -> Any:
        if self._coordinator is None:
            return self._no_coordinator()
        try:
            tools = await self._coordinator.list_tools(
                node_id, operator_id=self._operator_id(), owner_id=self._owner_id
            )
        except ConnectorError as exc:
            return self._map_error(exc)
        if not tools:
            return ToolResult(
                "This device has no registered tools. Ask the user to register one "
                "with `nanobot-connector tool add` on that computer."
            )
        normalized_tools = [
            {
                **tool,
                # Older connector clients do not send completion; their historic
                # behavior is always to wait for the launched process to exit.
                "completion": tool.get("completion", "wait"),
                **_local_approval_state(tool, category="exec"),
                **_completion_safety(tool),
            }
            for tool in tools
            if isinstance(tool, dict)
        ]
        return json.dumps(
            {
                "tools": normalized_tools,
                "approvalSemantics": {
                    "local": "本机限时预授权检查；未授权会立即拒绝，不会等待网页端按钮。localApprovalState 是该工具的实时授权状态：armed=主人已在设备上授权（含剩余时间），可直接调用；not armed=会被立即拒绝，需主人在设备上 arm；unknown=连接器版本过旧不上报。",
                    "webui": "服务端会等待 WebUI 用户逐次批准，直到批准或超时。",
                    "auto": "不需要额外审批，仍受既有授权和限流约束。",
                },
                "completionSemantics": {
                    "wait": "等待本机进程结束并回传输出；浏览器、QQ 等常驻程序会一直占用调用。",
                    "launch": "仅确认本机程序已启动后立即返回；不等待退出，也不回传程序输出。",
                },
            },
            ensure_ascii=False,
        )


@tool_parameters({
    "type": "object",
    "properties": {
        "node_id": {"type": "string", "description": "Target device id from connector_list_nodes (required — never guess)."},
        "tool": {"type": "string", "description": "Tool name from connector_list_tools."},
        "args": {"type": "object", "description": "Structured arguments matching the tool's parameters.", "additionalProperties": True},
    },
    "required": ["node_id", "tool"],
    "additionalProperties": False,
})
class ConnectorCallToolTool(_ConnectorExecTool):
    name = "connector_call_tool"
    description = (
        "Run a registered tool on a paired local device and return its output. The "
        "device owner must have registered and authorized the tool; some tools "
        "require the owner to approve each run. Discover tools with connector_list_tools. "
        "Never call a completion=wait tool for a persistent GUI application such as QQ or a "
        "browser: ask the owner to configure completion=launch first."
    )

    async def execute(self, node_id: str, tool: str, args: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        if self._coordinator is None:
            return self._no_coordinator()
        try:
            result = await self._coordinator.call_tool(
                node_id, tool, args or {},
                operator_id=self._operator_id(), owner_id=self._owner_id,
            )
        except ConnectorError as exc:
            return self._map_error(exc)
        payload = {
            "exitCode": result.exit_code,
            "durationMs": result.duration_ms,
            "timedOut": result.timed_out,
            "truncated": result.truncated,
            "cancelled": result.cancelled,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
        return json.dumps(payload, ensure_ascii=False)


# --- local MCP proxy (add-connector-mcp-proxy) ---------------------------


class _ConnectorMcpTool(_ConnectorExecTool):
    """Base for bridged-MCP tools: gated on ``enabled`` + ``allowExec`` +
    ``allowMcpProxy`` (MCP proxy is an execution-class capability)."""

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        cfg = getattr(ctx, "connector_config", None)
        return bool(
            cfg is not None
            and cfg.enabled
            and getattr(cfg, "allow_exec", False)
            and getattr(cfg, "allow_mcp_proxy", False)
        )


@tool_parameters({
    "type": "object",
    "properties": {
        "node_id": {"type": "string", "description": "Target device id from connector_list_nodes."},
    },
    "required": ["node_id"],
    "additionalProperties": False,
})
class ConnectorListMcpToolsTool(_ConnectorMcpTool):
    name = "connector_list_mcp_tools"
    description = (
        "List tools from the MCP servers a paired device bridges to the server. "
        "Returns each tool's server, name, description, and approval policy "
        "(local-approval tools also carry a live localApprovalState). Use "
        "before connector_call_mcp_tool."
    )

    async def execute(self, node_id: str, **kwargs: Any) -> Any:
        if self._coordinator is None:
            return self._no_coordinator()
        try:
            tools = await self._coordinator.list_mcp_tools(
                node_id, operator_id=self._operator_id(), owner_id=self._owner_id
            )
        except ConnectorError as exc:
            return self._map_error(exc)
        if not tools:
            return ToolResult(
                "This device bridges no MCP tools. Ask the user to register a local "
                "MCP server with `nanobot-connector mcp add` on that computer."
            )
        normalized = [
            {**tool, **_local_approval_state(tool, category="mcp")}
            for tool in tools
            if isinstance(tool, dict)
        ]
        return json.dumps({"tools": normalized}, ensure_ascii=False)


@tool_parameters({
    "type": "object",
    "properties": {
        "node_id": {"type": "string", "description": "Target device id (required — never guess)."},
        "server": {"type": "string", "description": "Bridged MCP server name from connector_list_mcp_tools."},
        "tool": {"type": "string", "description": "Tool name from connector_list_mcp_tools."},
        "args": {"type": "object", "description": "Structured arguments per the tool's input schema.", "additionalProperties": True},
    },
    "required": ["node_id", "server", "tool"],
    "additionalProperties": False,
})
class ConnectorCallMcpToolTool(_ConnectorMcpTool):
    name = "connector_call_mcp_tool"
    description = (
        "Call a tool exposed by an MCP server bridged from a paired device, and "
        "return its result. Discover tools with connector_list_mcp_tools. Subject "
        "to the same authorization and approval as other device tools."
    )

    async def execute(self, node_id: str, server: str, tool: str, args: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        if self._coordinator is None:
            return self._no_coordinator()
        try:
            result = await self._coordinator.call_mcp_tool(
                node_id, server, tool, args or {},
                operator_id=self._operator_id(), owner_id=self._owner_id,
            )
        except ConnectorError as exc:
            return self._map_error(exc)
        return json.dumps(result, ensure_ascii=False)


# --- desktop control (add-connector-desktop-control) ---------------------


class _ConnectorDesktopTool(_ConnectorTool):
    """Base for desktop-control tools: gated on ``enabled`` + ``allowDesktopControl``
    (independent of allowExec), routed through the shared DesktopSessionManager."""

    read_only = False

    def __init__(self, *, connector_config: Any, workspace: Path, manager: Any = None,
                 hub: Any | None = None, owner_id: str = _DEFAULT_OWNER) -> None:
        super().__init__(connector_config=connector_config, workspace=workspace,
                         hub=hub, owner_id=owner_id)
        self._explicit_manager = manager

    @property
    def _manager(self) -> Any:
        """Resolve the shared desktop manager lazily (same build-order reason as
        the exec coordinator: tools are constructed before the gateway registers)."""
        if self._explicit_manager is not None:
            return self._explicit_manager
        from nanobot.connector.desktop import default_desktop_manager

        return default_desktop_manager()

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        cfg = getattr(ctx, "connector_config", None)
        return bool(cfg is not None and cfg.enabled and getattr(cfg, "allow_desktop_control", False))

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(connector_config=ctx.connector_config, workspace=Path(ctx.workspace))

    def _operator_id(self) -> str:
        return self._owner_id

    def _no_manager(self) -> ToolResult:
        return ToolResult.error(
            "Desktop control is not available on this server (connector.allowDesktopControl is off)."
        )


@tool_parameters({
    "type": "object",
    "properties": {
        "node_id": {"type": "string", "description": "Target device id (required — never guess)."},
        "goal": {"type": "string", "description": "What you intend to do on the desktop (shown to the owner for consent)."},
    },
    "required": ["node_id", "goal"],
    "additionalProperties": False,
})
class ConnectorDesktopSessionTool(_ConnectorDesktopTool):
    name = "connector_desktop_session"
    description = (
        "Start a controlled desktop-control session on a paired device to operate "
        "GUI apps. Requires the device owner to approve on their machine. Returns a "
        "session_id and the first screenshot; then use connector_desktop_act to click/"
        "type based on the screenshot, and connector_desktop_end when done."
    )

    async def execute(self, node_id: str, goal: str, **kwargs: Any) -> Any:
        if self._manager is None:
            return self._no_manager()
        try:
            result = await self._manager.start(
                node_id, operator_id=self._operator_id(), owner_id=self._owner_id, goal=goal
            )
        except ConnectorError as exc:
            return self._map_error(exc)
        return json.dumps(result, ensure_ascii=False)


@tool_parameters({
    "type": "object",
    "properties": {
        "session_id": {"type": "string", "description": "Session id from connector_desktop_session."},
        "action": {
            "type": "object",
            "description": "One action: {type: click|double_click|right_click|type|key|scroll|drag|move|wait, x, y, text, ...}.",
            "additionalProperties": True,
        },
    },
    "required": ["session_id", "action"],
    "additionalProperties": False,
})
class ConnectorDesktopActTool(_ConnectorDesktopTool):
    name = "connector_desktop_act"
    description = (
        "Perform one mouse/keyboard action in an active desktop session and return "
        "the next screenshot. Sensitive actions (pay/confirm/delete/password) require "
        "the device owner to confirm before they run."
    )

    async def execute(self, session_id: str, action: dict[str, Any], **kwargs: Any) -> Any:
        if self._manager is None:
            return self._no_manager()
        try:
            result = await self._manager.act(session_id, action or {}, operator_id=self._operator_id())
        except ConnectorError as exc:
            return self._map_error(exc)
        return json.dumps(result, ensure_ascii=False)


@tool_parameters({
    "type": "object",
    "properties": {
        "session_id": {"type": "string", "description": "Session id to end."},
    },
    "required": ["session_id"],
    "additionalProperties": False,
})
class ConnectorDesktopEndTool(_ConnectorDesktopTool):
    name = "connector_desktop_end"
    description = "End an active desktop-control session, stopping capture and input on the device."

    async def execute(self, session_id: str, **kwargs: Any) -> Any:
        if self._manager is None:
            return self._no_manager()
        try:
            result = await self._manager.end(session_id, operator_id=self._operator_id())
        except ConnectorError as exc:
            return self._map_error(exc)
        return json.dumps(result, ensure_ascii=False)
