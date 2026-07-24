"""Connector client: outbound WSS, register, RPC dispatch, reconnect.

Only outbound connections are made — the connector never listens on a port.
Reconnect uses exponential backoff with jitter (1s→60s). A ``revoked`` frame
stops the loop; a ``cancel`` frame aborts an in-flight transfer.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import math
import platform
import random
import socket
import ssl
import time
from collections.abc import Awaitable, Callable
from urllib.parse import urlencode, urlsplit

import websockets
from websockets.asyncio.client import ClientConnection

from nanobot_connector import audit
from nanobot_connector import protocol as proto
from nanobot_connector.config import ConnectorClientConfig
from nanobot_connector.desktop import DesktopController, DesktopError, default_controller
from nanobot_connector.files import (
    FetchChunker,
    FileService,
    NotFoundError,
    NotTextError,
    PathDeniedError,
    TooLargeError,
    normalize_roots,
)
from nanobot_connector.logbuf import log_event
from nanobot_connector.mcp_bridge import McpBridge, McpRegistry
from nanobot_connector.runner import launch_execution, run_execution
from nanobot_connector.tools import ToolDef, ToolError, ToolRegistry

_BACKOFF_MIN = 1.0
_BACKOFF_MAX = 60.0

# Local-approval decision for an ``approval=local`` tool: returns True to allow.
LocalApprovalHook = Callable[[ToolDef, dict], Awaitable[bool]]

# Live arm-window lookup for a category ("exec" | "mcp" | "desktop"): returns the
# remaining seconds, 0 when not armed. Lets tools.list/mcp.list report whether an
# approval=local tool would pass the on-device consent check *right now*.
ArmedRemainingHook = Callable[[str], int]


class RevokedError(Exception):
    """Raised when the server revokes this device; the client should stop."""


def machine_fingerprint() -> str:
    """Stable per-machine id: hostname + platform node, hashed."""
    raw = f"{socket.gethostname()}|{platform.node()}|{platform.machine()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _ssl_context(cfg: ConnectorClientConfig, scheme: str) -> ssl.SSLContext | None:
    if scheme != "wss":
        return None
    ctx = ssl.create_default_context()
    if cfg.insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    elif cfg.cert_fingerprint:
        # Pin by fingerprint: disable chain/hostname checks, verify the cert
        # digest ourselves after the handshake.
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _check_transport_fingerprint(transport: object, expected_fp: str) -> None:
    """Compare the peer cert sha256 on *transport* against *expected_fp*."""
    der = None
    ssl_obj = transport.get_extra_info("ssl_object") if transport else None
    if ssl_obj is not None:
        der = ssl_obj.getpeercert(binary_form=True)
    if der is None:
        raise ssl.SSLError("could not read server certificate for pinning")
    actual = hashlib.sha256(der).hexdigest()
    if actual.lower() != expected_fp.replace(":", "").lower():
        raise ssl.SSLError(
            f"server cert fingerprint mismatch: expected {expected_fp}, got {actual}"
        )


def _pinned_connection_factory(expected_fp: str) -> type:
    """A ClientConnection that verifies the pinned cert *before* any data is sent.

    ``connection_made`` fires right after the TLS handshake and before the HTTP
    upgrade request (which carries the device token in its URL). Aborting there
    guarantees the token is never disclosed to a server with the wrong cert.
    """

    class PinnedClientConnection(ClientConnection):
        def connection_made(self, transport) -> None:  # noqa: ANN001
            _check_transport_fingerprint(transport, expected_fp)
            super().connection_made(transport)

    return PinnedClientConnection


class ConnectorClient:
    def __init__(
        self,
        cfg: ConnectorClientConfig,
        *,
        registry: ToolRegistry | None = None,
        on_local_approval: LocalApprovalHook | None = None,
        armed_remaining: ArmedRemainingHook | None = None,
        mcp_bridge: McpBridge | None = None,
        desktop: DesktopController | None = None,
    ):
        self.cfg = cfg
        self.roots = normalize_roots(cfg.roots)
        self.files = FileService(self.roots)
        self.registry = registry if registry is not None else ToolRegistry.load()
        self._on_local_approval = on_local_approval
        self._armed_remaining = armed_remaining
        # MCP bridge: present only if the owner registered any local MCP servers.
        self.mcp_bridge = mcp_bridge if mcp_bridge is not None else _load_mcp_bridge()
        # Desktop controller: present only if the owner opted into desktop control.
        self.desktop = desktop if desktop is not None else _load_desktop(cfg)
        self._cancelled: set[str] = set()
        self._fetch_tasks: set[asyncio.Task] = set()
        self._exec_tasks: set[asyncio.Task] = set()
        self._exec_cancels: dict[str, asyncio.Event] = {}

    def _ws_url(self) -> tuple[str, str]:
        parts = urlsplit(self.cfg.server)
        scheme = parts.scheme or "wss"
        base = f"{scheme}://{parts.netloc}{parts.path or '/connector/ws'}"
        query = urlencode({"device_token": self.cfg.device_token})
        return f"{base}?{query}", scheme

    async def run_forever(self) -> None:
        audit.prune()
        if self.mcp_bridge is not None:
            await self.mcp_bridge.start()
        backoff = _BACKOFF_MIN
        try:
            while True:
                try:
                    await self._connect_once()
                    backoff = _BACKOFF_MIN
                except RevokedError:
                    log_event("设备已被服务端吊销，请重新配对后再连接。", "error")
                    return
                except (OSError, websockets.WebSocketException, ssl.SSLError) as exc:
                    log_event(f"连接断开：{exc}；{backoff:.0f} 秒后重试", "warn")
                except Exception as exc:  # noqa: BLE001 - keep the daemon alive
                    log_event(f"出现未预期错误：{exc}；{backoff:.0f} 秒后重试", "error")
                await asyncio.sleep(backoff + random.uniform(0, backoff * 0.25))
                backoff = min(backoff * 2, _BACKOFF_MAX)
        finally:
            if self.mcp_bridge is not None:
                await self.mcp_bridge.stop()

    async def _connect_once(self) -> None:
        url, scheme = self._ws_url()
        ssl_ctx = _ssl_context(self.cfg, scheme)
        kwargs: dict = {}
        if scheme == "wss" and self.cfg.cert_fingerprint and not self.cfg.insecure:
            kwargs["create_connection"] = _pinned_connection_factory(self.cfg.cert_fingerprint)
        log_event(f"正在连接 {url} …")
        async with websockets.connect(url, ssl=ssl_ctx, max_size=None, **kwargs) as ws:
            await self._register(ws)
            await self._serve(ws)

    async def _register(self, ws) -> None:
        capabilities = [proto.CAP_FS, proto.CAP_EXEC]
        if self.mcp_bridge is not None:
            capabilities.append(proto.CAP_MCP)
        if self.desktop is not None:
            capabilities.append(proto.CAP_DESKTOP)
        node = {
            "name": self.cfg.name or socket.gethostname(),
            "platform": platform.system().lower(),
            "version": _client_version(),
            "roots": [str(r) for r in self.roots],
            "fingerprint": self.cfg.fingerprint or machine_fingerprint(),
            "capabilities": capabilities,
        }
        await ws.send(json.dumps(proto.register_frame(node), ensure_ascii=False))

    async def _serve(self, ws) -> None:
        heartbeat = asyncio.create_task(self._heartbeat(ws))
        try:
            async for raw in ws:
                frame = json.loads(raw)
                await self._dispatch(ws, frame)
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            # Capture/input must stop the instant the controlling server is gone.
            if self.desktop is not None and self.desktop.active:
                self.desktop.end_session()
            for evt in list(self._exec_cancels.values()):
                evt.set()  # unblock executors so they terminate their process trees
            for task in list(self._fetch_tasks) + list(self._exec_tasks):
                task.cancel()
            for task in list(self._fetch_tasks) + list(self._exec_tasks):
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            self._fetch_tasks.clear()
            self._exec_tasks.clear()
            self._exec_cancels.clear()

    async def _heartbeat(self, ws) -> None:
        while True:
            await asyncio.sleep(20)
            with contextlib.suppress(Exception):
                await ws.send(json.dumps(proto.heartbeat_frame(int(time.time()))))

    async def _dispatch(self, ws, frame: dict) -> None:
        ftype = frame.get("type")
        if ftype == "registered":
            log_event(f"已连接并注册为 {frame.get('nodeId')}，等待服务端指令。")
            return
        if ftype == "revoked":
            raise RevokedError()
        if ftype == "cancel":
            rpc_id = frame.get("id", "")
            self._cancelled.add(rpc_id)
            evt = self._exec_cancels.get(rpc_id)
            if evt is not None:
                evt.set()
            return
        if ftype == "rpc_request":
            await self._handle_rpc(ws, frame)

    async def _handle_rpc(self, ws, frame: dict) -> None:
        rpc_id = frame.get("id", "")
        method = frame.get("method", "")
        params = frame.get("params", {}) or {}
        path = params.get("path", "")
        try:
            if method == "fs.list":
                result = self.files.list_dir(path, params.get("max_entries", 500))
            elif method == "fs.stat":
                result = self.files.stat(path)
            elif method == "fs.search":
                result = self.files.search(params.get("query", ""), path)
            elif method == "fs.read":
                result = self.files.read_text(path, params.get("max_bytes", 262_144))
                audit.record("fs.read", path, bytes_count=len(result["content"]))
            elif method == "fs.fetch":
                # Run in the background so the read loop keeps processing
                # frames (notably `cancel`) while chunks are being sent.
                task = asyncio.create_task(self._handle_fetch(ws, rpc_id, path))
                self._fetch_tasks.add(task)
                task.add_done_callback(self._fetch_tasks.discard)
                return
            elif method == "tools.list":
                result = {"tools": self._annotate_armed(self.registry.list_public(), "exec")}
            elif method == "tools.call":
                # Background task: streams exec_output and ends with exec_result,
                # while the read loop stays free to receive tools.cancel.
                task = asyncio.create_task(self._handle_exec(ws, rpc_id, params))
                self._exec_tasks.add(task)
                task.add_done_callback(self._exec_tasks.discard)
                return
            elif method == "tools.cancel":
                target = params.get("id", "") or params.get("target", "")
                self._cancelled.add(target)
                evt = self._exec_cancels.get(target)
                if evt is not None:
                    evt.set()
                result = {"cancelled": target}
            elif method == "mcp.list":
                if self.mcp_bridge is not None:
                    result = {
                        "tools": self._annotate_armed(self.mcp_bridge.list_tools(), "mcp"),
                        "servers": self.mcp_bridge.server_health(),
                    }
                else:
                    result = {"tools": [], "servers": []}
            elif method == "mcp.call":
                task = asyncio.create_task(self._handle_mcp_call(ws, rpc_id, params))
                self._exec_tasks.add(task)
                task.add_done_callback(self._exec_tasks.discard)
                return
            elif method in (
                "desktop.session.start", "desktop.session.end",
                "desktop.capture", "desktop.input",
            ):
                task = asyncio.create_task(self._handle_desktop(ws, rpc_id, method, params))
                self._exec_tasks.add(task)
                task.add_done_callback(self._exec_tasks.discard)
                return
            else:
                await ws.send(json.dumps(proto.rpc_error(rpc_id, proto.ERROR_INTERNAL, "unknown method")))
                return
        except PathDeniedError as exc:
            audit.record(method, path, result="path_denied")
            await ws.send(json.dumps(proto.rpc_error(rpc_id, proto.ERROR_PATH_DENIED, str(exc))))
            return
        except NotFoundError as exc:
            await ws.send(json.dumps(proto.rpc_error(rpc_id, proto.ERROR_NOT_FOUND, str(exc))))
            return
        except NotTextError as exc:
            await ws.send(json.dumps(proto.rpc_error(rpc_id, proto.ERROR_NOT_TEXT, str(exc))))
            return
        except TooLargeError as exc:
            await ws.send(json.dumps(proto.rpc_error(rpc_id, proto.ERROR_TOO_LARGE, str(exc))))
            return
        except OSError as exc:
            await ws.send(json.dumps(proto.rpc_error(rpc_id, proto.ERROR_INTERNAL, str(exc))))
            return
        await ws.send(json.dumps(proto.rpc_response(rpc_id, result), ensure_ascii=False))

    async def _handle_fetch(self, ws, rpc_id: str, path: str) -> None:
        try:
            target, size = self.files.open_for_fetch(path, self.cfg.max_file_bytes)
        except PathDeniedError as exc:
            audit.record("fs.fetch", path, result="path_denied")
            await ws.send(json.dumps(proto.rpc_error(rpc_id, proto.ERROR_PATH_DENIED, str(exc))))
            return
        except NotFoundError as exc:
            await ws.send(json.dumps(proto.rpc_error(rpc_id, proto.ERROR_NOT_FOUND, str(exc))))
            return
        except TooLargeError as exc:
            await ws.send(json.dumps(proto.rpc_error(rpc_id, proto.ERROR_TOO_LARGE, str(exc))))
            return
        chunker = FetchChunker(target, rpc_id, self.cfg.chunk_bytes, total_bytes=size)
        try:
            for chunk_frame in chunker.frames():
                if rpc_id in self._cancelled:
                    self._cancelled.discard(rpc_id)
                    audit.record("fs.fetch", path, result="cancelled")
                    return
                await ws.send(json.dumps(chunk_frame))
                await asyncio.sleep(0)  # cooperative yield for cancel/heartbeat
        except OSError as exc:
            audit.record("fs.fetch", path, result="error")
            with contextlib.suppress(Exception):
                await ws.send(json.dumps(proto.rpc_error(rpc_id, proto.ERROR_INTERNAL, str(exc))))
            return
        audit.record("fs.fetch", path, bytes_count=size)

    async def _handle_exec(self, ws, rpc_id: str, params: dict) -> None:
        """Validate → (local approval) → run a registered tool, streaming output.

        Pre-start refusals go back as ``rpc_error`` (never ran); a started process
        streams ``exec_output`` and always ends with one ``exec_result``.
        """
        name = params.get("tool", "") or params.get("name", "")
        args = params.get("args", {}) or {}
        try:
            argv, env_overlay = self.registry.render(name, args)
            tool = self.registry.get(name)
        except ToolError as exc:
            audit.record(f"tools.call:{name}", "", result=exc.code)
            await ws.send(json.dumps(proto.rpc_error(rpc_id, exc.code, exc.message)))
            return

        if tool.approval == "local" and not await self._confirm_local(tool, args):
            audit.record(f"tools.call:{name}", "", result=proto.ERROR_APPROVAL_DENIED)
            log_event(f"已拒绝执行「{name}」：本机未在授权窗口内授权", "warn")
            await ws.send(json.dumps(
                proto.rpc_error(rpc_id, proto.ERROR_APPROVAL_DENIED, "denied on device")
            ))
            return

        log_event(f"开始执行工具「{name}」")
        cancel_event = asyncio.Event()
        self._exec_cancels[rpc_id] = cancel_event
        if rpc_id in self._cancelled:  # cancel arrived during validation/approval
            cancel_event.set()

        async def on_output(stream: str, text: str, seq: int) -> None:
            with contextlib.suppress(Exception):
                await ws.send(json.dumps(proto.exec_output(rpc_id, stream, seq, text)))

        timeout_s = float(tool.timeout_s or self.cfg.exec_timeout_s)
        try:
            if tool.completion == "launch":
                log_event(f"工具「{name}」已作为常驻程序启动，不等待退出。")
                outcome = await launch_execution(
                    argv,
                    env_overlay=env_overlay,
                    workdir=tool.workdir,
                )
            else:
                outcome = await run_execution(
                    argv,
                    env_overlay=env_overlay,
                    workdir=tool.workdir,
                    timeout_s=timeout_s,
                    max_output_bytes=self.cfg.max_exec_output_bytes,
                    on_output=on_output,
                    cancel_event=cancel_event,
                )
        except OSError as exc:
            audit.record(f"tools.call:{name}", "", result="error")
            with contextlib.suppress(Exception):
                await ws.send(json.dumps(proto.rpc_error(rpc_id, proto.ERROR_INTERNAL, str(exc))))
            return
        except Exception as exc:  # noqa: BLE001 - a background task must always resolve its RPC
            # ``tools.call`` is tracked by the server as an execution stream. If
            # this task escapes unexpectedly, no exec_result/rpc_error reaches the
            # hub and the caller waits for the full server execution timeout.
            audit.record(f"tools.call:{name}", "", result="internal_error")
            log_event(f"工具「{name}」执行器异常：{exc}", "error")
            with contextlib.suppress(Exception):
                await ws.send(json.dumps(proto.rpc_error(
                    rpc_id,
                    proto.ERROR_INTERNAL,
                    "connector client failed while running the tool",
                )))
            return
        finally:
            self._exec_cancels.pop(rpc_id, None)
            self._cancelled.discard(rpc_id)

        audit.record(
            f"tools.call:{name}", "",
            result="cancelled" if outcome.cancelled else ("timeout" if outcome.timed_out else "ok"),
        )
        if outcome.cancelled:
            log_event(f"工具「{name}」已被取消", "warn")
        elif outcome.timed_out:
            log_event(f"工具「{name}」执行超时", "warn")
        else:
            log_event(f"工具「{name}」执行完成（退出码 {outcome.exit_code}）")
        await ws.send(json.dumps(proto.exec_result(
            rpc_id,
            exit_code=outcome.exit_code,
            duration_ms=outcome.duration_ms,
            timed_out=outcome.timed_out,
            truncated=outcome.truncated,
            cancelled=outcome.cancelled,
        )))

    async def _handle_mcp_call(self, ws, rpc_id: str, params: dict) -> None:
        """Forward one bridged MCP tool call to the local MCP server."""
        server = params.get("server", "")
        tool = params.get("tool", "") or params.get("name", "")
        args = params.get("args", {}) or {}
        if self.mcp_bridge is None:
            await ws.send(json.dumps(
                proto.rpc_error(rpc_id, proto.ERROR_MCP_UNAVAILABLE, "no MCP servers bridged")
            ))
            return
        try:
            result = await self.mcp_bridge.call_tool(server, tool, args)
        except ConnectionError as exc:
            await ws.send(json.dumps(proto.rpc_error(rpc_id, proto.ERROR_MCP_UNAVAILABLE, str(exc))))
            return
        except PermissionError as exc:
            await ws.send(json.dumps(proto.rpc_error(rpc_id, proto.ERROR_APPROVAL_DENIED, str(exc))))
            return
        except Exception as exc:  # noqa: BLE001 - relay as internal error
            await ws.send(json.dumps(proto.rpc_error(rpc_id, proto.ERROR_INTERNAL, str(exc))))
            return
        await ws.send(json.dumps(proto.rpc_response(rpc_id, result), ensure_ascii=False))

    async def _handle_desktop(self, ws, rpc_id: str, method: str, params: dict) -> None:
        """Handle a desktop control RPC inside the on-device session guard."""
        if self.desktop is None:
            await ws.send(json.dumps(
                proto.rpc_error(rpc_id, proto.ERROR_DESKTOP_UNSUPPORTED, "desktop control not enabled")
            ))
            return
        session_id = params.get("sessionId", "") or params.get("session_id", "")
        try:
            if method == "desktop.session.start":
                await self.desktop.start_session(
                    session_id, operator=params.get("operator", ""), goal=params.get("goal", ""),
                    max_dimension=params.get("maxDimension"), max_fps=params.get("maxFps"),
                )
                log_event(f"桌面会话已开始（目标：{params.get('goal', '') or '未说明'}）", "warn")
                result: dict = {"started": session_id}
            elif method == "desktop.session.end":
                self.desktop.end_session()
                log_event("桌面会话已结束")
                result = {"ended": True}
            elif method == "desktop.capture":
                loop = asyncio.get_running_loop()
                frame = await loop.run_in_executor(None, lambda: self.desktop.capture(session_id))
                result = {
                    "image": frame.image_b64, "width": frame.width,
                    "height": frame.height, "format": frame.fmt,
                }
            else:  # desktop.input
                self.desktop.inject(session_id, params.get("action", {}) or {})
                result = {"ok": True}
        except DesktopError as exc:
            await ws.send(json.dumps(proto.rpc_error(rpc_id, exc.code, exc.message)))
            return
        except Exception as exc:  # noqa: BLE001 - relay as internal error
            await ws.send(json.dumps(proto.rpc_error(rpc_id, proto.ERROR_INTERNAL, str(exc))))
            return
        await ws.send(json.dumps(proto.rpc_response(rpc_id, result), ensure_ascii=False))

    def _annotate_armed(self, tools: list[dict], category: str) -> list[dict]:
        """Stamp each ``approval=local`` tool with the owner's live arm window.

        ``armedRemainingS`` > 0 means a call passes the on-device consent check
        right now; 0 means it would be refused. Tools with other approval
        policies — and every tool when no arm store is wired — carry no field,
        matching older connectors (absent == status unknown).
        """
        if self._armed_remaining is None:
            return tools
        try:
            raw_remaining = self._armed_remaining(category)
            if isinstance(raw_remaining, bool) or not isinstance(raw_remaining, (int, float)):
                raise ValueError("授权状态返回了非数值剩余时间")
            if not math.isfinite(raw_remaining):
                raise ValueError("授权状态返回了无效剩余时间")
            remaining = max(0, math.ceil(raw_remaining))
        except Exception as exc:  # noqa: BLE001 - display metadata must not break an RPC
            log_event(f"无法读取本机授权状态；工具列表将显示为状态未知：{exc}", "warn")
            return tools
        return [
            {**tool, "armedRemainingS": remaining}
            if isinstance(tool, dict) and tool.get("approval") == "local"
            else tool
            for tool in tools
        ]

    async def _confirm_local(self, tool: ToolDef, args: dict) -> bool:
        """Ask the device owner to approve an ``approval=local`` tool.

        Fail-closed: with no approval handler wired (e.g. headless daemon without a
        tray), a ``local`` tool is denied rather than run unattended.
        """
        if self._on_local_approval is None:
            return False
        try:
            return bool(await self._on_local_approval(tool, args))
        except Exception:  # noqa: BLE001 - a broken handler must not auto-approve
            return False


def _load_mcp_bridge(on_local_approval=None) -> McpBridge | None:
    """Build an MCP bridge only if the owner registered local MCP servers."""
    try:
        registry = McpRegistry.load()
    except Exception:  # noqa: BLE001 - a broken mcp.json must not break the daemon
        return None
    if not registry.list():
        return None
    return McpBridge(registry, on_local_approval=on_local_approval)


def build_daemon_client(cfg: ConnectorClientConfig) -> "ConnectorClient":
    """Construct the daemon client with on-device consent wired to the arm store.

    ``local``-approval tools/MCP and desktop sessions are fail-closed; here they are
    granted only while the owner has armed the matching category via the CLI
    (``nanobot-connector arm <exec|mcp|desktop> --for ...``).
    """
    from nanobot_connector.arm import ArmStore

    arm = ArmStore()

    def is_armed_safely(category: str) -> bool:
        try:
            return arm.is_armed(category)
        except Exception as exc:  # noqa: BLE001 - corrupted local state must fail closed
            log_event(f"无法读取本机授权状态，已拒绝 {category} 操作：{exc}", "warn")
            return False

    async def approve_exec(tool, args):
        return is_armed_safely("exec")

    async def approve_mcp(server, tool, args):
        return is_armed_safely("mcp")

    async def authorize_desktop(operator, goal):
        return is_armed_safely("desktop")

    def indicator(active: bool) -> None:
        log_event("● 屏幕正在被远程捕获（桌面控制进行中）" if active
                  else "○ 屏幕捕获已停止")

    bridge = _load_mcp_bridge(on_local_approval=approve_mcp)
    desktop = None
    if getattr(cfg, "desktop_enabled", False):
        desktop = default_controller(
            max_fps=getattr(cfg, "desktop_max_fps", 2),
            max_dimension=getattr(cfg, "desktop_max_dimension", 1280),
            on_local_authorize=authorize_desktop,
            on_indicator=indicator,
        )
    return ConnectorClient(
        cfg, on_local_approval=approve_exec, armed_remaining=arm.remaining,
        mcp_bridge=bridge, desktop=desktop,
    )


def _load_desktop(cfg: ConnectorClientConfig) -> DesktopController | None:
    """Build a desktop controller only if the owner opted into desktop control."""
    if not getattr(cfg, "desktop_enabled", False):
        return None
    return default_controller(
        max_fps=getattr(cfg, "desktop_max_fps", 2),
        max_dimension=getattr(cfg, "desktop_max_dimension", 1280),
    )


def _client_version() -> str:
    from nanobot_connector import __version__

    return __version__
