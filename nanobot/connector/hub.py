"""ConnectorHub: node registry + RPC routing over the WS data channel.

The hub is a process-global singleton (mirroring
``DEFAULT_EXEC_SESSION_MANAGER``) so the gateway WS handler and the agent-side
``connector_*`` tools share one registry without threading a reference through
``ToolContext`` and ``AgentLoop``.

Responsibilities:

- ``serve``: run one connector connection's lifecycle (register → read loop),
  routing ``rpc_response`` / ``file_chunk`` / ``heartbeat`` frames.
- ``rpc``: send an ``rpc_request`` and await the matching ``rpc_response``.
- ``fetch_file``: send ``fs.fetch`` and assemble the returned ``file_chunk``
  stream into the workspace landing zone.
- On disconnect, every pending call fails with :class:`ConnectorDisconnectedError`.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Protocol

from loguru import logger

from nanobot.connector import protocol as proto
from nanobot.connector.transfer import (
    ChunkAssembler,
    TransferError,
    enforce_cache_quota,
    sanitize_landing_path,
)

_REGISTER_TIMEOUT_S = 10.0


class ConnectorError(Exception):
    """Base error for hub operations, carrying a stable ``code``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ConnectorDisconnectedError(ConnectorError):
    def __init__(self, message: str = "connector disconnected") -> None:
        super().__init__(proto.ERROR_NODE_OFFLINE, message)


class ConnectorTimeoutError(ConnectorError):
    def __init__(self, message: str = "connector rpc timed out") -> None:
        super().__init__(proto.ERROR_RPC_TIMEOUT, message)


class ConnectorRemoteError(ConnectorError):
    """The connector returned ``rpc_response(ok=false)``."""


class Connection(Protocol):
    """Minimal duplex the hub needs; satisfied by ``websockets`` server conns."""

    async def send(self, data: str) -> None: ...

    def __aiter__(self) -> AsyncIterator[str]: ...


async def _close_connection(connection: Connection) -> None:
    close = getattr(connection, "close", None)
    if callable(close):
        result = close()
        if asyncio.iscoroutine(result):
            await result


@dataclass
class _Transfer:
    queue: asyncio.Queue[proto.FileChunkFrame | ConnectorError]


@dataclass
class _Exec:
    """In-flight ``tools.call``: the device streams ``exec_output`` then one
    ``exec_result`` (or a pre-start ``rpc_response(ok=false)``) onto this queue."""

    queue: "asyncio.Queue[proto.ExecOutputFrame | proto.ExecResultFrame | ConnectorError]"


@dataclass
class ExecResult:
    """Terminal outcome of a controlled execution, surfaced to the caller.

    ``stdout``/``stderr`` hold the accumulated text (bounded by the server output
    cap) so a non-streaming caller (e.g. an agent tool) gets the result directly;
    streaming callers also receive each chunk live via the ``on_output`` callback.
    """

    exit_code: int | None
    duration_ms: int
    timed_out: bool = False
    truncated: bool = False
    cancelled: bool = False
    stdout: str = ""
    stderr: str = ""


@dataclass
class ConnectorNode:
    node_id: str
    owner_id: str
    info: proto.NodeInfo
    connection: Connection
    last_seen: float = field(default_factory=time.monotonic)
    _pending_rpc: dict[str, asyncio.Future[Any]] = field(default_factory=dict)
    _transfers: dict[str, _Transfer] = field(default_factory=dict)
    _execs: dict[str, _Exec] = field(default_factory=dict)

    @property
    def capabilities(self) -> list[str]:
        return list(self.info.capabilities)

    def supports(self, capability: str) -> bool:
        return capability in self.info.capabilities

    def public(self) -> dict[str, Any]:
        return {
            "nodeId": self.node_id,
            "ownerId": self.owner_id,
            "name": self.info.name,
            "platform": self.info.platform,
            "version": self.info.version,
            "roots": list(self.info.roots),
            "capabilities": list(self.info.capabilities),
        }


class ConnectorHub:
    def __init__(self) -> None:
        self._nodes: dict[str, ConnectorNode] = {}
        self._lock = asyncio.Lock()

    # -- node lifecycle -----------------------------------------------------

    async def serve(
        self,
        connection: Connection,
        *,
        node_id: str,
        owner_id: str,
        heartbeat_interval_s: int = 20,
        on_seen: Any = None,
    ) -> None:
        """Drive one connector connection from register to disconnect.

        ``on_seen`` (optional) is called with ``node_id`` on register and each
        heartbeat, so the caller can persist ``lastSeenAt``.
        """
        node = await self._await_register(
            connection, node_id=node_id, owner_id=owner_id,
            heartbeat_interval_s=heartbeat_interval_s,
        )
        if node is None:
            return
        async with self._lock:
            old = self._nodes.get(node_id)
            if old is not None and old.connection is not connection:
                self._fail_node(old, ConnectorDisconnectedError("superseded by new connection"))
                await _close_connection(old.connection)
            self._nodes[node_id] = node
        if on_seen:
            on_seen(node_id)
        logger.info("connector: node {} online ({})", node_id, node.info.name)
        try:
            async for raw in connection:
                await self._route_incoming(node, raw, on_seen=on_seen)
        except Exception as exc:  # noqa: BLE001 - connection ended
            logger.debug("connector: node {} read loop ended: {}", node_id, exc)
        finally:
            await self._detach(node)

    async def _await_register(
        self,
        connection: Connection,
        *,
        node_id: str,
        owner_id: str,
        heartbeat_interval_s: int,
    ) -> ConnectorNode | None:
        it = connection.__aiter__()
        try:
            raw = await asyncio.wait_for(it.__anext__(), timeout=_REGISTER_TIMEOUT_S)
        except (asyncio.TimeoutError, StopAsyncIteration):
            logger.warning("connector: node {} did not register in time", node_id)
            return None
        try:
            frame = proto.parse_frame(json.loads(raw))
        except (ValueError, proto.ProtocolError):
            await self._send(connection, proto.ErrorFrame(code=proto.ERROR_INTERNAL, message="bad register"))
            return None
        if not isinstance(frame, proto.RegisterFrame):
            await self._send(connection, proto.ErrorFrame(code=proto.ERROR_INTERNAL, message="expected register"))
            return None
        if frame.protocol > proto.PROTOCOL_VERSION:
            await self._send(
                connection,
                proto.ErrorFrame(code=proto.ERROR_PROTOCOL_UNSUPPORTED, message="protocol too new"),
            )
            return None
        await self._send(
            connection,
            proto.RegisteredFrame(node_id=node_id, heartbeat_interval_s=heartbeat_interval_s),
        )
        return ConnectorNode(
            node_id=node_id, owner_id=owner_id, info=frame.node, connection=connection
        )

    async def _detach(self, node: ConnectorNode) -> None:
        async with self._lock:
            # Only drop the registry entry if it still refers to *this* node
            # instance; a reconnect may already have replaced it.
            if self._nodes.get(node.node_id) is node:
                self._nodes.pop(node.node_id)
        self._fail_node(node, ConnectorDisconnectedError())
        logger.info("connector: node {} offline", node.node_id)

    def _fail_node(self, node: ConnectorNode, err: ConnectorError) -> None:
        for fut in node._pending_rpc.values():
            if not fut.done():
                fut.set_exception(err)
        node._pending_rpc.clear()
        for transfer in node._transfers.values():
            transfer.queue.put_nowait(err)
        node._transfers.clear()
        for execution in node._execs.values():
            execution.queue.put_nowait(err)
        node._execs.clear()

    async def disconnect_node(self, node_id: str, *, revoked: bool = False) -> bool:
        """Drop an online node; optionally notify the client with ``revoked``."""
        async with self._lock:
            node = self._nodes.get(node_id)
        if node is None:
            return False
        if revoked:
            with suppress(Exception):
                await self._send(node.connection, proto.RevokedFrame())
        await _close_connection(node.connection)
        await self._detach(node)
        return True

    async def _route_incoming(self, node: ConnectorNode, raw: str, *, on_seen: Any) -> None:
        try:
            frame = proto.parse_frame(json.loads(raw))
        except (ValueError, proto.ProtocolError):
            logger.debug("connector: node {} sent unparseable frame", node.node_id)
            return
        node.last_seen = time.monotonic()
        if isinstance(frame, proto.HeartbeatFrame):
            if on_seen:
                on_seen(node.node_id)
            return
        if isinstance(frame, proto.RpcResponseFrame):
            self._resolve_rpc(node, frame)
            return
        if isinstance(frame, proto.FileChunkFrame):
            transfer = node._transfers.get(frame.id)
            if transfer is not None:
                transfer.queue.put_nowait(frame)
            return
        if isinstance(frame, (proto.ExecOutputFrame, proto.ExecResultFrame)):
            execution = node._execs.get(frame.id)
            if execution is not None:
                execution.queue.put_nowait(frame)
            return

    def _resolve_rpc(self, node: ConnectorNode, frame: proto.RpcResponseFrame) -> None:
        # A failed fetch/exec is delivered as rpc_response(ok=false) on its queue.
        transfer = node._transfers.get(frame.id)
        if transfer is not None and not frame.ok:
            code = (frame.error or {}).get("code", proto.ERROR_INTERNAL)
            msg = (frame.error or {}).get("message", "remote error")
            transfer.queue.put_nowait(ConnectorRemoteError(code, msg))
            return
        execution = node._execs.get(frame.id)
        if execution is not None and not frame.ok:
            code = (frame.error or {}).get("code", proto.ERROR_INTERNAL)
            msg = (frame.error or {}).get("message", "remote error")
            execution.queue.put_nowait(ConnectorRemoteError(code, msg))
            return
        fut = node._pending_rpc.pop(frame.id, None)
        if fut is None or fut.done():
            return
        if frame.ok:
            fut.set_result(frame.result)
        else:
            code = (frame.error or {}).get("code", proto.ERROR_INTERNAL)
            msg = (frame.error or {}).get("message", "remote error")
            fut.set_exception(ConnectorRemoteError(code, msg))

    # -- public queries -----------------------------------------------------

    def list_nodes(self, *, owner_id: str | None = None) -> list[dict[str, Any]]:
        nodes = list(self._nodes.values())
        if owner_id is not None:
            nodes = [n for n in nodes if n.owner_id == owner_id]
        return [n.public() for n in nodes]

    def _get_node(self, node_id: str, *, owner_id: str | None) -> ConnectorNode:
        node = self._nodes.get(node_id)
        if node is None:
            raise ConnectorDisconnectedError(f"node {node_id} is offline")
        if owner_id is not None and node.owner_id != owner_id:
            # Do not leak existence across owners.
            raise ConnectorDisconnectedError(f"node {node_id} is offline")
        return node

    # -- RPC ----------------------------------------------------------------

    async def rpc(
        self,
        node_id: str,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float,
        owner_id: str | None = None,
    ) -> Any:
        node = self._get_node(node_id, owner_id=owner_id)
        rpc_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        node._pending_rpc[rpc_id] = fut
        await self._send(
            node.connection, proto.RpcRequestFrame(id=rpc_id, method=method, params=params)
        )
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError as exc:
            node._pending_rpc.pop(rpc_id, None)
            raise ConnectorTimeoutError() from exc

    async def fetch_file(
        self,
        node_id: str,
        client_path: str,
        *,
        base_dir: Path,
        max_file_bytes: int,
        fetch_cache_max_bytes: int,
        timeout: float,
        owner_id: str | None = None,
        max_concurrent_transfers: int = 2,
    ) -> Path:
        """Fetch a remote file into ``<base_dir>/<node_id>/`` and return its path."""
        node = self._get_node(node_id, owner_id=owner_id)
        if len(node._transfers) >= max_concurrent_transfers:
            raise ConnectorError(
                proto.ERROR_INTERNAL,
                "too many concurrent transfers on this device; retry shortly",
            )
        dest = sanitize_landing_path(base_dir, node_id, client_path)
        rpc_id = uuid.uuid4().hex
        transfer = _Transfer(queue=asyncio.Queue())
        node._transfers[rpc_id] = transfer
        assembler: ChunkAssembler | None = None
        try:
            await self._send(
                node.connection,
                proto.RpcRequestFrame(id=rpc_id, method="fs.fetch", params={"path": client_path}),
            )
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    await self._send(node.connection, proto.CancelFrame(id=rpc_id))
                    raise ConnectorTimeoutError("fetch timed out")
                try:
                    item = await asyncio.wait_for(transfer.queue.get(), timeout=remaining)
                except asyncio.TimeoutError as exc:
                    await self._send(node.connection, proto.CancelFrame(id=rpc_id))
                    raise ConnectorTimeoutError("fetch timed out") from exc
                if isinstance(item, ConnectorError):
                    raise item
                if assembler is None:
                    enforce_cache_quota(
                        base_dir, item.total_bytes or 0, fetch_cache_max_bytes
                    )
                    assembler = ChunkAssembler(dest=dest, max_file_bytes=max_file_bytes)
                if item.eof:
                    return assembler.finalize(
                        sha256=item.sha256, total_bytes=item.total_bytes
                    )
                assembler.add_chunk(item.seq, item.data)
        except TransferError as exc:
            if assembler is not None:
                assembler.abort()
            raise ConnectorError(exc.code, exc.message) from exc
        except BaseException:
            if assembler is not None:
                assembler.abort()
            raise
        finally:
            node._transfers.pop(rpc_id, None)

    # -- controlled execution (v2) -----------------------------------------

    async def list_tools(
        self,
        node_id: str,
        *,
        timeout: float,
        owner_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return the device's registered tool schemas (``tools.list``)."""
        node = self._get_node(node_id, owner_id=owner_id)
        if not node.supports(proto.CAP_EXEC):
            raise ConnectorError(
                proto.ERROR_EXEC_UNSUPPORTED,
                "device does not support controlled execution (upgrade the connector)",
            )
        result = await self.rpc(node_id, "tools.list", {}, timeout=timeout, owner_id=owner_id)
        if isinstance(result, dict):
            tools = result.get("tools", [])
            return list(tools) if isinstance(tools, list) else []
        return []

    async def call_tool(
        self,
        node_id: str,
        tool: str,
        args: dict[str, Any],
        *,
        timeout: float,
        max_output_bytes: int,
        owner_id: str | None = None,
        max_concurrent_execs: int = 2,
        on_output: Any = None,
        cancel_event: asyncio.Event | None = None,
    ) -> ExecResult:
        """Invoke a registered tool on the device, streaming output until it ends.

        Returns an :class:`ExecResult`; raises :class:`ConnectorError` for pre-start
        refusals (unknown tool, invalid args, denied) and disconnect/timeout. A
        started execution always resolves via an ``exec_result`` frame.
        """
        node = self._get_node(node_id, owner_id=owner_id)
        if not node.supports(proto.CAP_EXEC):
            raise ConnectorError(
                proto.ERROR_EXEC_UNSUPPORTED,
                "device does not support controlled execution (upgrade the connector)",
            )
        if len(node._execs) >= max_concurrent_execs:
            raise ConnectorError(
                proto.ERROR_EXEC_LIMIT,
                "too many concurrent executions on this device; retry shortly",
            )
        rpc_id = uuid.uuid4().hex
        execution = _Exec(queue=asyncio.Queue())
        node._execs[rpc_id] = execution
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        total = 0
        server_truncated = False
        cancel_sent = False
        poll_s = 0.25
        try:
            await self._send(
                node.connection,
                proto.RpcRequestFrame(id=rpc_id, method="tools.call",
                                      params={"tool": tool, "args": args or {}}),
            )
            deadline = time.monotonic() + timeout
            while True:
                if cancel_event is not None and cancel_event.is_set() and not cancel_sent:
                    await self._send(node.connection, proto.CancelFrame(id=rpc_id))
                    cancel_sent = True  # await the device's exec_result(cancelled)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    await self._send(node.connection, proto.CancelFrame(id=rpc_id))
                    raise ConnectorTimeoutError("execution timed out")
                try:
                    item = await asyncio.wait_for(
                        execution.queue.get(), timeout=min(remaining, poll_s)
                    )
                except asyncio.TimeoutError:
                    continue  # re-check cancel / deadline
                if isinstance(item, ConnectorError):
                    raise item
                if isinstance(item, proto.ExecOutputFrame):
                    if total < max_output_bytes:
                        room = max_output_bytes - total
                        clip = item.data[:room]
                        total += len(clip)
                        (stderr_parts if item.stream == "stderr" else stdout_parts).append(clip)
                        if len(item.data) > room:
                            server_truncated = True
                    else:
                        server_truncated = True
                    if on_output is not None:
                        await on_output(item.stream, item.data, item.seq)
                    continue
                if isinstance(item, proto.ExecResultFrame):
                    return ExecResult(
                        exit_code=item.exit_code,
                        duration_ms=item.duration_ms,
                        timed_out=item.timed_out,
                        truncated=item.truncated or server_truncated,
                        cancelled=item.cancelled,
                        stdout="".join(stdout_parts),
                        stderr="".join(stderr_parts),
                    )
        except asyncio.CancelledError:
            # The caller (e.g. an aborted agent turn) went away — tell the device
            # to stop so the process tree does not orphan and keep consuming.
            with suppress(Exception):
                await self._send(node.connection, proto.CancelFrame(id=rpc_id))
            raise
        finally:
            node._execs.pop(rpc_id, None)

    # -- local MCP proxy (v2.5) --------------------------------------------

    async def list_mcp_tools(
        self,
        node_id: str,
        *,
        timeout: float,
        owner_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return the device's bridged MCP tools (``mcp.list``)."""
        node = self._get_node(node_id, owner_id=owner_id)
        if not node.supports(proto.CAP_MCP):
            raise ConnectorError(
                proto.ERROR_MCP_UNSUPPORTED,
                "device does not bridge any local MCP servers",
            )
        result = await self.rpc(node_id, "mcp.list", {}, timeout=timeout, owner_id=owner_id)
        if isinstance(result, dict):
            tools = result.get("tools", [])
            return list(tools) if isinstance(tools, list) else []
        return []

    async def mcp_status(
        self,
        node_id: str,
        *,
        timeout: float,
        owner_id: str | None = None,
    ) -> dict[str, Any]:
        """Return the device's bridged MCP tools + per-server health (for WebUI)."""
        node = self._get_node(node_id, owner_id=owner_id)
        if not node.supports(proto.CAP_MCP):
            raise ConnectorError(
                proto.ERROR_MCP_UNSUPPORTED, "device does not bridge any local MCP servers"
            )
        result = await self.rpc(node_id, "mcp.list", {}, timeout=timeout, owner_id=owner_id)
        if not isinstance(result, dict):
            return {"tools": [], "servers": []}
        return {"tools": result.get("tools", []) or [], "servers": result.get("servers", []) or []}

    async def call_mcp_tool(
        self,
        node_id: str,
        server: str,
        tool: str,
        args: dict[str, Any],
        *,
        timeout: float,
        owner_id: str | None = None,
    ) -> dict[str, Any]:
        """Invoke a bridged MCP tool on the device (``mcp.call``)."""
        node = self._get_node(node_id, owner_id=owner_id)
        if not node.supports(proto.CAP_MCP):
            raise ConnectorError(
                proto.ERROR_MCP_UNSUPPORTED,
                "device does not bridge any local MCP servers",
            )
        result = await self.rpc(
            node_id, "mcp.call",
            {"server": server, "tool": tool, "args": args or {}},
            timeout=timeout, owner_id=owner_id,
        )
        return result if isinstance(result, dict) else {"content": str(result), "isError": False}

    # -- desktop control (v3) ----------------------------------------------

    async def desktop_rpc(
        self,
        node_id: str,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float,
        owner_id: str | None = None,
    ) -> Any:
        """Route a ``desktop.*`` method to the device, checking the capability."""
        node = self._get_node(node_id, owner_id=owner_id)
        if not node.supports(proto.CAP_DESKTOP):
            raise ConnectorError(
                proto.ERROR_DESKTOP_UNSUPPORTED,
                "device does not support desktop control (enable it in the connector)",
            )
        if method not in proto.DESKTOP_METHODS:
            raise ConnectorError(proto.ERROR_INTERNAL, f"not a desktop method: {method}")
        return await self.rpc(node_id, method, params, timeout=timeout, owner_id=owner_id)

    async def _send(self, connection: Connection, frame: proto._Frame) -> None:
        await connection.send(json.dumps(proto.dump_frame(frame), ensure_ascii=False))


_DEFAULT_HUB: ConnectorHub | None = None


def default_hub() -> ConnectorHub:
    """Return the process-global hub, creating it on first use."""
    global _DEFAULT_HUB
    if _DEFAULT_HUB is None:
        _DEFAULT_HUB = ConnectorHub()
    return _DEFAULT_HUB
