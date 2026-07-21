"""Execution orchestration for controlled local tools (add-connector-local-tools).

Sits between the agent tool / gateway and the :class:`ConnectorHub`, enforcing the
production controls that must not live on the (untrusted) device:

- **rate limiting**: per-operator and per-device sliding windows.
- **authorization**: cross-person grants (existence-hiding: unauthorized == not found).
- **approval**: ``webui`` policy holds the call at a broker until a WebUI user
  approves; the TTL is default-deny. (``local`` is enforced on the device; ``auto``
  passes straight through.)
- **metrics + audit**: every attempt is counted and written to the exec audit log.

Process-global singletons (broker/metrics/limiter) mirror :func:`default_hub` so the
gateway HTTP routes and the agent-side tools share one coordinator.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.connector import protocol as proto
from nanobot.connector.authz import AuthorizationStore
from nanobot.connector.hub import ConnectorError, ConnectorHub, ExecResult

_APPROVAL_POLL_S = 0.2


# --- rate limiting -------------------------------------------------------


class RateLimiter:
    """Fixed-window-ish sliding rate limiter keyed by an arbitrary string."""

    def __init__(self, *, per_minute: int) -> None:
        self._per_minute = per_minute
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def check_and_record(self, key: str) -> bool:
        """Return True if allowed (and record the hit); False if over the limit."""
        now = time.monotonic()
        cutoff = now - 60.0
        async with self._lock:
            q = self._events[key]
            while q and q[0] < cutoff:
                q.popleft()
            if len(q) >= self._per_minute:
                return False
            q.append(now)
            return True


# --- metrics -------------------------------------------------------------


@dataclass
class ExecMetrics:
    """In-memory execution counters for monitoring/alerting."""

    executions: int = 0
    failures: int = 0
    approval_denied: int = 0
    rate_limited: int = 0
    by_node: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    _durations_ms: deque[int] = field(default_factory=lambda: deque(maxlen=1024))

    def record_execution(self, node_id: str, *, duration_ms: int, ok: bool) -> None:
        self.executions += 1
        self.by_node[node_id] += 1
        self._durations_ms.append(duration_ms)
        if not ok:
            self.failures += 1

    def record_approval_denied(self) -> None:
        self.approval_denied += 1

    def record_rate_limited(self) -> None:
        self.rate_limited += 1

    def snapshot(self) -> dict[str, Any]:
        durations = sorted(self._durations_ms)
        p50 = durations[len(durations) // 2] if durations else 0
        p95 = durations[int(len(durations) * 0.95)] if durations else 0
        failure_rate = (self.failures / self.executions) if self.executions else 0.0
        return {
            "executions": self.executions,
            "failures": self.failures,
            "failureRate": round(failure_rate, 4),
            "approvalDenied": self.approval_denied,
            "rateLimited": self.rate_limited,
            "durationMsP50": p50,
            "durationMsP95": p95,
            "byNode": dict(self.by_node),
        }


# --- webui approval broker ----------------------------------------------


@dataclass
class PendingApproval:
    approval_id: str
    node_id: str
    tool: str
    operator_id: str
    args_summary: dict[str, Any]
    created_at: float
    _event: asyncio.Event = field(default_factory=asyncio.Event)
    _approved: bool = False

    def public(self) -> dict[str, Any]:
        return {
            "approvalId": self.approval_id,
            "nodeId": self.node_id,
            "tool": self.tool,
            "operatorId": self.operator_id,
            "args": self.args_summary,
            "createdAt": int(self.created_at),
        }


class ApprovalBroker:
    """Holds ``webui``-policy executions until a WebUI user approves or the TTL
    elapses. Default-deny: an unresolved approval at TTL is treated as rejected."""

    def __init__(self) -> None:
        self._pending: dict[str, PendingApproval] = {}
        self._lock = asyncio.Lock()

    async def create(
        self, *, approval_id: str, node_id: str, tool: str, operator_id: str,
        args_summary: dict[str, Any],
    ) -> PendingApproval:
        pending = PendingApproval(
            approval_id=approval_id, node_id=node_id, tool=tool,
            operator_id=operator_id, args_summary=args_summary, created_at=time.time(),
        )
        async with self._lock:
            self._pending[approval_id] = pending
        return pending

    async def wait(self, pending: PendingApproval, *, ttl_s: float) -> bool:
        """Block until resolved or TTL. Returns True only on explicit approval."""
        try:
            await asyncio.wait_for(pending._event.wait(), timeout=ttl_s)
        except asyncio.TimeoutError:
            return False  # default-deny
        finally:
            async with self._lock:
                self._pending.pop(pending.approval_id, None)
        return pending._approved

    async def resolve(self, approval_id: str, *, approved: bool) -> bool:
        """Resolve a pending approval. Returns True if one was found.

        Who may resolve (must be the device owner, and — in multi-user — not the
        requesting operator) is enforced at the gateway route, which is the auth
        boundary and knows device ownership. The broker only holds/releases.
        """
        async with self._lock:
            pending = self._pending.get(approval_id)
            if pending is None:
                return False
            pending._approved = approved
            pending._event.set()
            return True

    def pending_node(self, approval_id: str) -> str | None:
        """Node id of a pending approval, so the route can check device ownership."""
        pending = self._pending.get(approval_id)
        return pending.node_id if pending is not None else None

    def list_pending(self) -> list[dict[str, Any]]:
        return [p.public() for p in self._pending.values()]


# --- audit ---------------------------------------------------------------


def audit_exec(
    workspace_path: Path,
    *,
    operator_id: str | None,
    node_id: str,
    tool: str,
    args_summary: dict[str, Any],
    approval: str,
    result: str,
    exit_code: int | None = None,
    duration_ms: int = 0,
) -> None:
    """Append one exec audit record (mirrors connector file-access audit)."""
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "operatorId": operator_id or "",
        "nodeId": node_id,
        "tool": tool,
        "args": args_summary,
        "approval": approval,
        "result": result,
        "exitCode": exit_code,
        "durationMs": duration_ms,
    }
    logger.info(
        "connector exec audit: operator={} node={} tool={} approval={} result={} exit={}",
        record["operatorId"], node_id, tool, approval, result, exit_code,
    )
    audit_path = Path(workspace_path) / "connector" / "exec-audit.log"
    try:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        with open(audit_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning("connector exec audit write failed: {}", exc)


# Param names that are redacted even without an explicit schema flag (defense in
# depth for the pre-schema audit points).
_SENSITIVE_HINTS = ("password", "passwd", "pwd", "secret", "token", "apikey",
                    "api_key", "credential", "auth")


def read_exec_audit(
    workspace_path: Path,
    *,
    node_ids: set[str] | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return the most recent exec-audit records (newest first).

    ``node_ids`` (when given) restricts to those devices — the gateway passes the
    owner's devices so the audit view is scoped by ownership.
    """
    audit_path = Path(workspace_path) / "connector" / "exec-audit.log"
    try:
        lines = audit_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in reversed(lines):  # newest first
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if node_ids is not None and record.get("nodeId") not in node_ids:
            continue
        records.append(record)
        if len(records) >= limit:
            break
    return records


def mcp_tool_key(server: str, tool: str) -> str:
    """Stable per-device authorization key for a bridged MCP tool.

    Kept distinct from local-tool names (which are bare) so grants never collide
    across the two surfaces.
    """
    return f"mcp:{server}:{tool}"


def summarize_args(
    args: dict[str, Any],
    *,
    sensitive_names: frozenset[str] = frozenset(),
    keep: int = 40,
) -> dict[str, Any]:
    """Redact/shorten arg values for audit and approval cards (never full secrets).

    A value is masked entirely if its param is declared ``sensitive`` in the tool
    schema, or if its name matches a built-in sensitive hint; otherwise it is
    truncated. This keeps passwords/tokens passed as arguments out of the audit
    log and approval UI.
    """
    out: dict[str, Any] = {}
    for k, v in (args or {}).items():
        kl = k.lower()
        if k in sensitive_names or any(h in kl for h in _SENSITIVE_HINTS):
            out[k] = "***"
            continue
        s = str(v)
        out[k] = s if len(s) <= keep else s[:keep] + "…"
    return out


# --- coordinator ---------------------------------------------------------


class ExecutionCoordinator:
    """Ties rate limit → authorization → approval → hub execution → metrics/audit."""

    def __init__(
        self,
        *,
        hub: ConnectorHub,
        authz: AuthorizationStore,
        workspace: Path,
        config: Any,
        metrics: ExecMetrics | None = None,
        broker: ApprovalBroker | None = None,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        self._hub = hub
        self._authz = authz
        self._workspace = Path(workspace)
        self._config = config
        self._metrics = metrics or default_exec_metrics()
        self._broker = broker or default_approval_broker()
        self._rate = rate_limiter or RateLimiter(per_minute=config.exec_rate_per_minute)

    async def list_tools(self, node_id: str, *, operator_id: str, owner_id: str) -> list[dict[str, Any]]:
        tools = await self._hub.list_tools(
            node_id, timeout=self._config.rpc_timeout_s, owner_id=owner_id
        )
        if operator_id == owner_id:
            return tools  # owner sees every registered tool
        # Cross-person: only surface tools this operator is granted, so tool
        # existence never leaks beyond what the owner authorized.
        return [
            t for t in tools
            if self._authz.is_authorized(
                node_id, t.get("name", ""), operator_id=operator_id, owner_id=owner_id
            )
        ]

    async def call_tool(
        self,
        node_id: str,
        tool: str,
        args: dict[str, Any],
        *,
        operator_id: str,
        owner_id: str,
        on_output: Any = None,
        cancel_event: asyncio.Event | None = None,
    ) -> ExecResult:
        args = args or {}
        # Before we know the schema, mask via the built-in hints only.
        summary = summarize_args(args)

        # 1) rate limit (per operator and per device)
        for key in (f"op:{operator_id}", f"node:{node_id}"):
            if not await self._rate.check_and_record(key):
                self._metrics.record_rate_limited()
                self._audit(operator_id, node_id, tool, summary, "-", "rate_limited")
                raise ConnectorError(proto.ERROR_EXEC_LIMIT, "execution rate limit exceeded; retry shortly")

        # 2) authorization — unauthorized cross-person is indistinguishable from
        #    "no such tool" so tool existence never leaks across operators.
        if not self._authz.is_authorized(node_id, tool, operator_id=operator_id, owner_id=owner_id):
            self._audit(operator_id, node_id, tool, summary, "-", proto.ERROR_EXEC_DENIED)
            raise ConnectorError(proto.ERROR_TOOL_NOT_FOUND, f"tool not available: {tool}")

        # 3) resolve approval policy + sensitive params from the device tool schema
        tools = await self._hub.list_tools(
            node_id, timeout=self._config.rpc_timeout_s, owner_id=owner_id
        )
        schema = next((t for t in tools if t.get("name") == tool), None)
        if schema is None:
            raise ConnectorError(proto.ERROR_TOOL_NOT_FOUND, f"tool not available: {tool}")
        approval = schema.get("approval", "local")
        sensitive = frozenset(
            p.get("name", "") for p in schema.get("params", []) if p.get("sensitive")
        )
        # Re-summarize now that we know which params the tool marked sensitive.
        summary = summarize_args(args, sensitive_names=sensitive)

        # 4) webui approval (default-deny on TTL). local is enforced on-device; auto passes.
        if approval == "webui":
            import uuid

            pending = await self._broker.create(
                approval_id=uuid.uuid4().hex, node_id=node_id, tool=tool,
                operator_id=operator_id, args_summary=summary,
            )
            approved = await self._broker.wait(pending, ttl_s=self._config.approval_ttl_s)
            if not approved:
                self._metrics.record_approval_denied()
                self._audit(operator_id, node_id, tool, summary, approval, proto.ERROR_APPROVAL_DENIED)
                raise ConnectorError(proto.ERROR_APPROVAL_DENIED, "execution not approved")

        # 5) execute on the device
        try:
            result = await self._hub.call_tool(
                node_id, tool, args,
                timeout=self._config.exec_timeout_s,
                max_output_bytes=self._config.max_exec_output_bytes,
                owner_id=owner_id,
                max_concurrent_execs=self._config.max_concurrent_execs,
                on_output=on_output,
                cancel_event=cancel_event,
            )
        except ConnectorError as exc:
            self._audit(operator_id, node_id, tool, summary, approval, exc.code)
            raise

        result_str = (
            "cancelled" if result.cancelled
            else "timeout" if result.timed_out
            else "ok" if result.exit_code == 0 else "nonzero_exit"
        )
        ok = result.exit_code == 0 and not result.cancelled and not result.timed_out
        self._metrics.record_execution(node_id, duration_ms=result.duration_ms, ok=ok)
        self._audit(
            operator_id, node_id, tool, summary, approval, result_str,
            exit_code=result.exit_code, duration_ms=result.duration_ms,
        )
        return result

    def _audit(self, operator_id, node_id, tool, summary, approval, result, **kw) -> None:
        audit_exec(
            self._workspace, operator_id=operator_id, node_id=node_id, tool=tool,
            args_summary=summary, approval=approval, result=result, **kw,
        )

    # -- local MCP proxy (v2.5) --------------------------------------------

    async def list_mcp_tools(
        self, node_id: str, *, operator_id: str, owner_id: str
    ) -> list[dict[str, Any]]:
        tools = await self._hub.list_mcp_tools(
            node_id, timeout=self._config.rpc_timeout_s, owner_id=owner_id
        )
        if operator_id == owner_id:
            return tools
        return [
            t for t in tools
            if self._authz.is_authorized(
                node_id, mcp_tool_key(t.get("server", ""), t.get("name", "")),
                operator_id=operator_id, owner_id=owner_id,
            )
        ]

    async def call_mcp_tool(
        self,
        node_id: str,
        server: str,
        tool: str,
        args: dict[str, Any],
        *,
        operator_id: str,
        owner_id: str,
    ) -> dict[str, Any]:
        """Invoke a bridged MCP tool with the same governance as a local tool:
        rate limit → authorization → webui approval → device (local enforced there)."""
        args = args or {}
        tool_key = mcp_tool_key(server, tool)
        summary = summarize_args(args)

        for key in (f"op:{operator_id}", f"node:{node_id}"):
            if not await self._rate.check_and_record(key):
                self._metrics.record_rate_limited()
                self._audit(operator_id, node_id, tool_key, summary, "-", "rate_limited")
                raise ConnectorError(proto.ERROR_EXEC_LIMIT, "execution rate limit exceeded; retry shortly")

        if not self._authz.is_authorized(node_id, tool_key, operator_id=operator_id, owner_id=owner_id):
            self._audit(operator_id, node_id, tool_key, summary, "-", proto.ERROR_EXEC_DENIED)
            raise ConnectorError(proto.ERROR_TOOL_NOT_FOUND, f"tool not available: {tool}")

        tools = await self._hub.list_mcp_tools(
            node_id, timeout=self._config.rpc_timeout_s, owner_id=owner_id
        )
        schema = next(
            (t for t in tools if t.get("server") == server and t.get("name") == tool), None
        )
        if schema is None:
            raise ConnectorError(proto.ERROR_TOOL_NOT_FOUND, f"tool not available: {tool}")
        approval = schema.get("approval", "local")

        if approval == "webui":
            import uuid

            pending = await self._broker.create(
                approval_id=uuid.uuid4().hex, node_id=node_id, tool=tool_key,
                operator_id=operator_id, args_summary=summary,
            )
            approved = await self._broker.wait(pending, ttl_s=self._config.approval_ttl_s)
            if not approved:
                self._metrics.record_approval_denied()
                self._audit(operator_id, node_id, tool_key, summary, approval, proto.ERROR_APPROVAL_DENIED)
                raise ConnectorError(proto.ERROR_APPROVAL_DENIED, "execution not approved")

        started = time.monotonic()
        try:
            result = await self._hub.call_mcp_tool(
                node_id, server, tool, args,
                timeout=self._config.exec_timeout_s, owner_id=owner_id,
            )
        except ConnectorError as exc:
            self._audit(operator_id, node_id, tool_key, summary, approval, exc.code)
            raise
        duration_ms = int((time.monotonic() - started) * 1000)
        is_error = bool(result.get("isError"))
        self._metrics.record_execution(node_id, duration_ms=duration_ms, ok=not is_error)
        self._audit(
            operator_id, node_id, tool_key, summary, approval,
            "error" if is_error else "ok", duration_ms=duration_ms,
        )
        return result


# --- process-global singletons ------------------------------------------

_DEFAULT_METRICS: ExecMetrics | None = None
_DEFAULT_BROKER: ApprovalBroker | None = None
_DEFAULT_COORDINATOR: ExecutionCoordinator | None = None


def default_exec_metrics() -> ExecMetrics:
    global _DEFAULT_METRICS
    if _DEFAULT_METRICS is None:
        _DEFAULT_METRICS = ExecMetrics()
    return _DEFAULT_METRICS


def default_approval_broker() -> ApprovalBroker:
    global _DEFAULT_BROKER
    if _DEFAULT_BROKER is None:
        _DEFAULT_BROKER = ApprovalBroker()
    return _DEFAULT_BROKER


def set_default_execution_coordinator(coordinator: ExecutionCoordinator) -> None:
    """Register the gateway's coordinator so agent-side tools share it.

    Sharing one instance is required for correctness: the WebUI approval route
    resolves pending approvals on this broker, and rate limits / metrics / grants
    must be the same the executing tool sees.
    """
    global _DEFAULT_COORDINATOR
    _DEFAULT_COORDINATOR = coordinator


def default_execution_coordinator() -> ExecutionCoordinator | None:
    return _DEFAULT_COORDINATOR
