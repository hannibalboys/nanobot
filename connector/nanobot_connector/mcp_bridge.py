"""Local MCP server bridge for the connector (add-connector-mcp-proxy).

The device owner registers locally-running MCP servers in ``~/.nanobot-connector/
mcp.json``. The connector acts as an MCP **client** to each of them and forwards
their tool list / calls to the nanobot server over the connector channel — the
server never connects to this machine and no port is exposed.

Each server's SDK contexts (stdio subprocess / http client + ClientSession) are
owned by a single asyncio task (AnyIO cancel-scope requirement, mirroring the
server's ``mcp.py``). Other tasks talk to it through a request queue + futures.
Credentials referenced by a server stay on this device (reusing the tools.py
:class:`SecretStore`) and are injected into the child's environment only.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

from nanobot_connector.config import config_dir
from nanobot_connector.tools import _NAME_RE, SecretStore

McpApproval = Literal["auto", "webui", "local"]
McpTransport = Literal["stdio", "sse", "streamableHttp"]

_STOP = object()


class McpServerDef(BaseModel):
    """One locally-registered MCP server (mirrors nanobot's MCPServerConfig shape)."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    name: str
    type: McpTransport | None = None  # auto-detected if omitted
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)  # literal, non-secret
    secrets: dict[str, str] = Field(default_factory=dict)  # env var -> secret id
    cwd: str = ""
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    approval: McpApproval = "local"  # safe default
    enabled_tools: list[str] = Field(default_factory=lambda: ["*"])

    @field_validator("name")
    @classmethod
    def _valid_name(cls, v: str) -> str:
        if not _NAME_RE.match(v):
            raise ValueError(f"invalid MCP server name: {v!r}")
        return v

    def transport(self) -> McpTransport:
        if self.type:
            return self.type
        if self.command:
            return "stdio"
        if self.url.rstrip("/").endswith("/sse"):
            return "sse"
        return "streamableHttp"


class McpRegistry:
    """Reads/writes ``mcp.json``. Only editable locally; never via the protocol."""

    def __init__(self, servers: list[McpServerDef] | None = None, *, path=None) -> None:
        self._path = path or (config_dir() / "mcp.json")
        self._servers: dict[str, McpServerDef] = {}
        for s in servers or []:
            self._servers[s.name] = s

    @classmethod
    def load(cls, *, path=None) -> "McpRegistry":
        import json

        reg = cls(path=path)
        try:
            data = json.loads(reg._path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return reg
        for row in data.get("servers", []) if isinstance(data, dict) else []:
            try:
                reg._servers[row["name"]] = McpServerDef.model_validate(row)
            except Exception:  # noqa: BLE001 - skip malformed, keep the rest
                continue
        return reg

    def save(self) -> None:
        import json
        import os

        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"servers": [s.model_dump(by_alias=True, exclude_defaults=True) for s in self._servers.values()]}
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)

    def add(self, server: McpServerDef) -> None:
        self._servers[server.name] = server

    def remove(self, name: str) -> bool:
        return self._servers.pop(name, None) is not None

    def list(self) -> list[McpServerDef]:
        return list(self._servers.values())


# --- MCP session abstraction (injectable for tests) ----------------------


class McpSession:
    """Minimal duplex the bridge needs; satisfied by ``mcp.ClientSession``."""

    async def initialize(self) -> None: ...  # pragma: no cover - protocol
    async def list_tools(self) -> Any: ...  # pragma: no cover - protocol
    async def call_tool(self, name: str, arguments: dict) -> Any: ...  # pragma: no cover - protocol


# session_factory(sdef, env) -> async context manager yielding an McpSession
SessionFactory = Callable[[McpServerDef, dict[str, str]], "AsyncIterator[McpSession]"]


@contextlib.asynccontextmanager
async def _default_session_factory(sdef: McpServerDef, env: dict[str, str]) -> AsyncIterator[McpSession]:
    """Real MCP client over stdio / SSE / streamable-HTTP (lazy-imports the SDK)."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.sse import sse_client
    from mcp.client.stdio import stdio_client
    from mcp.client.streamable_http import streamable_http_client

    transport = sdef.transport()
    async with contextlib.AsyncExitStack() as stack:
        if transport == "stdio":
            params = StdioServerParameters(
                command=sdef.command, args=list(sdef.args), env=env or None, cwd=sdef.cwd or None
            )
            read, write = await stack.enter_async_context(stdio_client(params))
        elif transport == "sse":
            read, write = await stack.enter_async_context(sse_client(sdef.url))
        else:
            read, write, _ = await stack.enter_async_context(streamable_http_client(sdef.url))
        session = await stack.enter_async_context(ClientSession(read, write))
        yield session


def _tool_public(tool: Any, *, server: str, approval: str) -> dict[str, Any]:
    return {
        "server": server,
        "name": getattr(tool, "name", ""),
        "description": getattr(tool, "description", "") or "",
        "inputSchema": getattr(tool, "inputSchema", None) or {},
        "approval": approval,
    }


def _flatten_result(result: Any) -> dict[str, Any]:
    """Reduce an MCP call result to ``{content, isError}`` text for transport."""
    parts: list[str] = []
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if text is not None:
            parts.append(text)
        else:
            parts.append(str(item))
    return {"content": "\n".join(parts), "isError": bool(getattr(result, "isError", False))}


class _ServerConn:
    """One local MCP server connection, owned by a single asyncio task."""

    def __init__(self, sdef: McpServerDef, env: dict[str, str], factory: SessionFactory) -> None:
        self.sdef = sdef
        self._env = env
        self._factory = factory
        self._queue: asyncio.Queue = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._pending: set[asyncio.Future] = set()
        self.healthy = False
        self.tools: list[dict[str, Any]] = []
        self.error: str | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is None:
            return
        await self._queue.put(_STOP)
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._task
        self.healthy = False

    async def request(self, method: str, params: dict) -> Any:
        if not self.healthy or self._task is None or self._task.done():
            raise ConnectionError(f"MCP server '{self.sdef.name}' unavailable")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        # Track the future so a disconnect that races with this enqueue still
        # resolves it (otherwise ``await fut`` could hang forever).
        self._pending.add(fut)
        fut.add_done_callback(self._pending.discard)
        await self._queue.put((method, params, fut))
        # If the worker exited between the health check and the enqueue, fail now
        # rather than await a future nothing will ever resolve.
        if self._task.done() and not fut.done():
            fut.set_exception(ConnectionError(f"MCP server '{self.sdef.name}' disconnected"))
        return await fut

    async def _run(self) -> None:
        try:
            async with self._factory(self.sdef, self._env) as session:
                await session.initialize()
                listed = await session.list_tools()
                self.tools = [
                    _tool_public(t, server=self.sdef.name, approval=self.sdef.approval)
                    for t in _selected_tools(listed, self.sdef)
                ]
                self.healthy = True
                self.error = None
                while True:
                    job = await self._queue.get()
                    if job is _STOP:
                        break
                    method, params, fut = job
                    try:
                        if method == "call_tool":
                            res = await session.call_tool(params["tool"], params.get("args") or {})
                            if not fut.done():
                                fut.set_result(_flatten_result(res))
                        else:
                            if not fut.done():
                                fut.set_exception(ValueError(f"unknown method {method}"))
                    except Exception as exc:  # noqa: BLE001 - relay to caller
                        if not fut.done():
                            fut.set_exception(exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - connection failed; mark unhealthy
            self.error = str(exc)
        finally:
            self.healthy = False
            self._drain_pending(ConnectionError(self.error or "disconnected"))

    def _drain_pending(self, err: Exception) -> None:
        # Fail anything still queued...
        while not self._queue.empty():
            job = self._queue.get_nowait()
            if job is _STOP:
                continue
            _method, _params, fut = job
            if not fut.done():
                fut.set_exception(err)
        # ...and any tracked future that never made it onto the queue (race).
        for fut in list(self._pending):
            if not fut.done():
                fut.set_exception(err)
        self._pending.clear()


def _selected_tools(listed: Any, sdef: McpServerDef) -> list[Any]:
    tools = list(getattr(listed, "tools", []) or [])
    enabled = set(sdef.enabled_tools)
    if "*" in enabled:
        return tools
    return [t for t in tools if getattr(t, "name", "") in enabled]


class McpBridge:
    """Manages all registered local MCP servers: connect, list, call, reconnect."""

    def __init__(
        self,
        registry: McpRegistry | None = None,
        *,
        secrets: SecretStore | None = None,
        session_factory: SessionFactory | None = None,
        reconnect_interval_s: float = 5.0,
        on_local_approval: Callable[[str, str, dict], Any] | None = None,
    ) -> None:
        self._registry = registry if registry is not None else McpRegistry.load()
        self._secrets = secrets or SecretStore()
        self._factory = session_factory or _default_session_factory
        self._conns: dict[str, _ServerConn] = {}
        self._monitor: asyncio.Task | None = None
        self._reconnect_interval_s = reconnect_interval_s
        # Local-approval gate for servers registered with approval="local".
        # Fail-closed: no handler ⇒ local-policy servers are denied on-device.
        self._on_local_approval = on_local_approval

    def _resolve_env(self, sdef: McpServerDef) -> dict[str, str]:
        env = dict(sdef.env)
        for var, secret_id in sdef.secrets.items():
            value = self._secrets.get(secret_id)
            if value is not None:
                env[var] = value
        return env

    async def start(self) -> None:
        for sdef in self._registry.list():
            conn = _ServerConn(sdef, self._resolve_env(sdef), self._factory)
            self._conns[sdef.name] = conn
            conn.start()
        self._monitor = asyncio.create_task(self._health_loop())

    async def stop(self) -> None:
        if self._monitor is not None:
            self._monitor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._monitor
            self._monitor = None
        for conn in list(self._conns.values()):
            await conn.stop()
        self._conns.clear()

    async def _health_loop(self) -> None:
        while True:
            await asyncio.sleep(self._reconnect_interval_s)
            for conn in list(self._conns.values()):
                if not conn.healthy and (conn._task is None or conn._task.done()):
                    conn.start()  # restart dead/unhealthy connection

    def list_tools(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for conn in self._conns.values():
            if conn.healthy:
                out.extend(conn.tools)
        return out

    def server_health(self) -> list[dict[str, Any]]:
        return [
            {"server": name, "healthy": conn.healthy, "toolCount": len(conn.tools), "error": conn.error}
            for name, conn in self._conns.items()
        ]

    def approval_for(self, server: str) -> str:
        """The approval policy of a bridged server (default 'local' if unknown)."""
        conn = self._conns.get(server)
        return conn.sdef.approval if conn is not None else "local"

    async def call_tool(self, server: str, tool: str, args: dict) -> dict[str, Any]:
        conn = self._conns.get(server)
        if conn is None or not conn.healthy:
            raise ConnectionError(f"MCP server '{server}' is not available")
        if conn.sdef.approval == "local":
            if self._on_local_approval is None:
                raise PermissionError("local approval required but no handler on device")
            approved = await self._on_local_approval(server, tool, args or {})
            if not approved:
                raise PermissionError("execution denied on device")
        return await conn.request("call_tool", {"tool": tool, "args": args or {}})
