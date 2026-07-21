"""Gateway-side glue for the connector: HTTP routes + WS serving.

This binds the :class:`DeviceStore`, :class:`PairRateLimiter`, and
:class:`ConnectorHub` to the WebSocket channel. The WebSocket channel delegates:

- ``/connector/ws`` handshakes to :meth:`authorize_ws` then :meth:`serve_ws`.
- ``/connector/pair`` and ``/api/connector/*`` HTTP requests to :meth:`handle_http`.

HTTP mutations use GET + query params to match the rest of the gateway surface
(the websockets ``process_request`` path exposes no request body).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.connector.authz import AuthorizationStore
from nanobot.connector.desktop import (
    DesktopSessionManager,
    delete_recording,
    list_recordings,
    read_desktop_audit,
    set_default_desktop_manager,
)
from nanobot.connector.devices import DeviceStore, PairRateLimiter
from nanobot.connector.downloads import connector_downloads_payload
from nanobot.connector.exec import (
    ExecutionCoordinator,
    default_approval_broker,
    default_exec_metrics,
    read_exec_audit,
    set_default_execution_coordinator,
)
from nanobot.connector.hub import ConnectorError, ConnectorHub, default_hub
from nanobot.webui.http_utils import (
    http_error as _http_error,
)
from nanobot.webui.http_utils import (
    http_json_response as _http_json_response,
)
from nanobot.webui.http_utils import (
    is_local_browser_request as _is_local_browser_request,
)
from nanobot.webui.http_utils import (
    issue_route_secret_matches as _issue_route_secret_matches,
)
from nanobot.webui.http_utils import (
    parse_query as _parse_query,
)
from nanobot.webui.http_utils import (
    query_first as _query_first,
)

# Single-owner deployments (shared WebUI access token) map to one owner id. The
# owner-isolation hooks in DeviceStore/ConnectorHub are ready for a future
# multi-user auth model.
_DEFAULT_OWNER = "webui"

_PAIRING_CODES_ROUTE = "/api/connector/pairing-codes"
_NODES_ROUTE = "/api/connector/nodes"
_REVOKE_ROUTE = "/api/connector/revoke"
_DOWNLOADS_ROUTE = "/api/connector/downloads"
_PAIR_ROUTE = "/connector/pair"
# v2 controlled-execution management routes
_ALIAS_ROUTE = "/api/connector/alias"
_TOOLS_ROUTE = "/api/connector/tools"
_GRANTS_ROUTE = "/api/connector/grants"
_GRANT_ROUTE = "/api/connector/grant"
_REVOKE_GRANT_ROUTE = "/api/connector/revoke-grant"
_REQUESTS_ROUTE = "/api/connector/requests"
_DENY_REQUEST_ROUTE = "/api/connector/deny-request"
_APPROVALS_ROUTE = "/api/connector/approvals"
_APPROVE_ROUTE = "/api/connector/approve"
_METRICS_ROUTE = "/api/connector/exec-metrics"
_EXEC_AUDIT_ROUTE = "/api/connector/exec-audit"
_MCP_TOOLS_ROUTE = "/api/connector/mcp-tools"
_DESKTOP_SESSIONS_ROUTE = "/api/connector/desktop-sessions"
_DESKTOP_TAKEOVER_ROUTE = "/api/connector/desktop-takeover"
_DESKTOP_AUDIT_ROUTE = "/api/connector/desktop-audit"
_DESKTOP_RECORDINGS_ROUTE = "/api/connector/desktop-recordings"
_DESKTOP_RECORDING_DELETE_ROUTE = "/api/connector/desktop-recording-delete"


class ConnectorGateway:
    def __init__(
        self,
        config: Any,
        *,
        workspace_path: Path,
        ws_config: Any,
        hub: ConnectorHub | None = None,
        tokens: Any | None = None,
        owner_id: str = _DEFAULT_OWNER,
    ) -> None:
        self.config = config
        self.ws_config = ws_config
        self.workspace_path = Path(workspace_path)
        self.hub = hub if hub is not None else default_hub()
        self._tokens = tokens
        self._owner_id = owner_id
        self.landing_dir = self.workspace_path / "connector"
        self.devices = DeviceStore(
            self.landing_dir / "devices.json",
            pairing_ttl_s=config.pairing_code_ttl_s,
        )
        self.rate_limiter = PairRateLimiter()
        # Controlled execution (v2): authorization store + coordinator. Wired even
        # when allow_exec is off so routes can 404 cleanly; call paths gate on it.
        self.authz = AuthorizationStore(self.landing_dir / "grants.json")
        self.broker = default_approval_broker()
        self.metrics = default_exec_metrics()
        self.coordinator = ExecutionCoordinator(
            hub=self.hub,
            authz=self.authz,
            workspace=self.workspace_path,
            config=config,
            metrics=self.metrics,
            broker=self.broker,
        )
        # Share this coordinator with the agent-side connector_* tools so grants,
        # approvals, rate limits, metrics, and audit are one consistent view.
        set_default_execution_coordinator(self.coordinator)
        # Desktop control (v3): session manager sharing the same authz + broker.
        self.desktop = DesktopSessionManager(
            hub=self.hub, authz=self.authz, workspace=self.workspace_path,
            config=config, broker=self.broker,
        )
        set_default_desktop_manager(self.desktop)

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled)

    @property
    def allow_exec(self) -> bool:
        return bool(self.config.enabled and getattr(self.config, "allow_exec", False))

    @property
    def allow_mcp_proxy(self) -> bool:
        return bool(self.allow_exec and getattr(self.config, "allow_mcp_proxy", False))

    @property
    def allow_desktop_control(self) -> bool:
        return bool(self.config.enabled and getattr(self.config, "allow_desktop_control", False))

    def matches_ws_path(self, path: str) -> bool:
        return self.enabled and path == self.config.path

    def owns_http_route(self, path: str) -> bool:
        if not self.enabled:
            return False
        if path in (
            _PAIR_ROUTE,
            _PAIRING_CODES_ROUTE,
            _NODES_ROUTE,
            _REVOKE_ROUTE,
            _DOWNLOADS_ROUTE,
            _ALIAS_ROUTE,
        ):
            return True
        # the MCP-tools route additionally requires allow_mcp_proxy
        if path == _MCP_TOOLS_ROUTE:
            return self.allow_mcp_proxy
        # desktop routes require allow_desktop_control
        if path in (
            _DESKTOP_SESSIONS_ROUTE, _DESKTOP_TAKEOVER_ROUTE, _DESKTOP_AUDIT_ROUTE,
            _DESKTOP_RECORDINGS_ROUTE, _DESKTOP_RECORDING_DELETE_ROUTE,
        ):
            return self.allow_desktop_control
        # exec-management routes exist only when allow_exec is on
        return self.allow_exec and path in (
            _TOOLS_ROUTE,
            _GRANTS_ROUTE,
            _GRANT_ROUTE,
            _REVOKE_GRANT_ROUTE,
            _REQUESTS_ROUTE,
            _DENY_REQUEST_ROUTE,
            _APPROVALS_ROUTE,
            _APPROVE_ROUTE,
            _METRICS_ROUTE,
            _EXEC_AUDIT_ROUTE,
        )

    # -- WS handshake + serving --------------------------------------------

    def authorize_ws(self, query: dict[str, list[str]]) -> Any | None:
        """Return the Device for a valid ``device_token`` query param, else None."""
        token = _query_first(query, "device_token") or ""
        return self.devices.verify_token(token)

    async def serve_ws(self, connection: Any, device: Any) -> None:
        """Run the connector connection lifecycle on the shared hub."""
        def _on_seen(node_id: str) -> None:
            self.devices.touch_last_seen(node_id)

        await self.hub.serve(
            connection,
            node_id=device.node_id,
            owner_id=device.owner_id,
            on_seen=_on_seen,
        )

    # -- HTTP ---------------------------------------------------------------

    def _authed(self, connection: Any, request: Any) -> bool:
        """Authorize a WebUI management request.

        Accepts, in order: a valid issued API token (the normal WebUI path), the
        configured shared secret as a Bearer token, or a local browser request.
        """
        if self._tokens is not None and self._tokens.check_api_token(request):
            return True
        secret = (
            self.ws_config.token_issue_secret.strip()
            or self.ws_config.token.strip()
        )
        if secret and _issue_route_secret_matches(request.headers, secret):
            return True
        return _is_local_browser_request(connection, request.headers)

    async def handle_http(self, connection: Any, request: Any, path: str) -> Any | None:
        query = _parse_query(request.path)

        if path == _PAIR_ROUTE:
            return self._handle_pair(connection, query)

        # management routes require WebUI auth
        if not self._authed(connection, request):
            return _http_error(401, "Unauthorized")

        if path == _PAIRING_CODES_ROUTE:
            return self._handle_generate_code()
        if path == _DOWNLOADS_ROUTE:
            return self._handle_downloads()
        if path == _NODES_ROUTE:
            return self._handle_list_nodes()
        if path == _REVOKE_ROUTE:
            return await self._handle_revoke(query)
        if path == _ALIAS_ROUTE:
            return self._handle_set_alias(query)

        # -- exec-management routes (allow_exec only) ----------------------
        if not self.allow_exec:
            return None
        if path == _TOOLS_ROUTE:
            return await self._handle_list_tools(query)
        if path == _GRANTS_ROUTE:
            return self._handle_list_grants(query)
        if path == _GRANT_ROUTE:
            return self._handle_grant(query)
        if path == _REVOKE_GRANT_ROUTE:
            return self._handle_revoke_grant(query)
        if path == _REQUESTS_ROUTE:
            return self._handle_list_requests(query)
        if path == _DENY_REQUEST_ROUTE:
            return self._handle_deny_request(query)
        if path == _APPROVALS_ROUTE:
            return self._handle_list_approvals()
        if path == _APPROVE_ROUTE:
            return await self._handle_approve(query)
        if path == _METRICS_ROUTE:
            return _http_json_response(self.metrics.snapshot())
        if path == _EXEC_AUDIT_ROUTE:
            return self._handle_exec_audit(query)
        if path == _MCP_TOOLS_ROUTE and self.allow_mcp_proxy:
            return await self._handle_mcp_tools(query)
        if path == _DESKTOP_SESSIONS_ROUTE and self.allow_desktop_control:
            return _http_json_response({"sessions": self.desktop.list_sessions(owner_id=self._owner_id)})
        if path == _DESKTOP_TAKEOVER_ROUTE and self.allow_desktop_control:
            return await self._handle_desktop_takeover(query)
        if path == _DESKTOP_AUDIT_ROUTE and self.allow_desktop_control:
            return self._handle_desktop_audit(query)
        if path == _DESKTOP_RECORDINGS_ROUTE and self.allow_desktop_control:
            return self._handle_desktop_recordings()
        if path == _DESKTOP_RECORDING_DELETE_ROUTE and self.allow_desktop_control:
            return self._handle_desktop_recording_delete(query)
        return None

    def _client_ip(self, connection: Any) -> str:
        remote = getattr(connection, "remote_address", None)
        if isinstance(remote, tuple) and remote:
            return str(remote[0])
        return str(remote or "unknown")

    def _handle_pair(self, connection: Any, query: dict[str, list[str]]) -> Any:
        ip = self._client_ip(connection)
        if self.rate_limiter.is_locked(ip):
            return _http_error(429, "too many attempts")

        code = _query_first(query, "code") or ""
        name = (_query_first(query, "name") or "unknown")[:128]
        platform = (_query_first(query, "platform") or "unknown")[:64]
        fingerprint = (_query_first(query, "fingerprint") or "")[:128]

        result = self.devices.redeem_pairing_code(
            code, name=name, platform=platform, machine_fingerprint=fingerprint
        )
        if result is None:
            tripped = self.rate_limiter.record_failure(ip)
            if tripped:
                voided = self.devices.invalidate_all_pending()
                logger.warning(
                    "connector: circuit breaker voided {} pending pairing codes", voided
                )
            return _http_error(401, "invalid or expired pairing code")

        self.rate_limiter.record_success(ip)
        node_id, token = result
        return _http_json_response({"nodeId": node_id, "token": token})

    def _handle_generate_code(self) -> Any:
        code, expires_at = self.devices.generate_pairing_code(self._owner_id)
        return _http_json_response({"code": code, "expiresAt": int(expires_at)})

    def _handle_downloads(self) -> Any:
        return _http_json_response(connector_downloads_payload(self.config))

    def _handle_list_nodes(self) -> Any:
        online_by_id = {
            n["nodeId"]: n for n in self.hub.list_nodes(owner_id=self._owner_id)
        }
        devices = self.devices.list_devices(owner_id=self._owner_id)
        return _http_json_response(
            {
                "nodes": [
                    {
                        **d.public(),
                        "online": d.node_id in online_by_id,
                        "roots": list(online_by_id.get(d.node_id, {}).get("roots", [])),
                    }
                    for d in devices
                ]
            }
        )

    async def _handle_revoke(self, query: dict[str, list[str]]) -> Any:
        node_id = _query_first(query, "nodeId") or ""
        ok = self.devices.revoke(node_id, owner_id=self._owner_id)
        if not ok:
            return _http_error(404, "device not found")
        await self.hub.disconnect_node(node_id, revoked=True)
        return _http_json_response({"revoked": node_id})

    # -- v2 controlled-execution management --------------------------------

    def _owned_node(self, node_id: str) -> bool:
        """True if *node_id* is a known device owned by this WebUI owner."""
        device = self.devices.get(node_id)
        return device is not None and device.owner_id == self._owner_id

    def _handle_set_alias(self, query: dict[str, list[str]]) -> Any:
        node_id = _query_first(query, "nodeId") or ""
        alias = _query_first(query, "alias") or ""
        if not self.devices.set_alias(node_id, alias, owner_id=self._owner_id):
            return _http_error(404, "device not found")
        return _http_json_response({"nodeId": node_id, "alias": alias.strip()[:128]})

    async def _handle_list_tools(self, query: dict[str, list[str]]) -> Any:
        node_id = _query_first(query, "nodeId") or ""
        if not self._owned_node(node_id):
            return _http_error(404, "device not found")
        try:
            tools = await self.coordinator.list_tools(
                node_id, operator_id=self._owner_id, owner_id=self._owner_id
            )
        except ConnectorError as exc:
            return _http_json_response({"error": exc.code, "message": exc.message}, status=409)
        return _http_json_response({"nodeId": node_id, "tools": tools})

    def _handle_list_grants(self, query: dict[str, list[str]]) -> Any:
        node_id = _query_first(query, "nodeId") or ""
        if not self._owned_node(node_id):
            return _http_error(404, "device not found")
        return _http_json_response({
            "grants": [g.public() for g in self.authz.list_grants(node_id=node_id)],
            "activeOperators": self.authz.active_operators(node_id),
        })

    def _handle_grant(self, query: dict[str, list[str]]) -> Any:
        node_id = _query_first(query, "nodeId") or ""
        tool = _query_first(query, "tool") or ""
        operator_id = _query_first(query, "operatorId") or ""
        ttl_raw = _query_first(query, "ttlS")
        if not self._owned_node(node_id):
            return _http_error(404, "device not found")
        if not tool or not operator_id:
            return _http_error(400, "tool and operatorId are required")
        ttl_s = int(ttl_raw) if ttl_raw and ttl_raw.isdigit() else None
        grant = self.authz.grant(node_id, tool, operator_id, granted_by=self._owner_id, ttl_s=ttl_s)
        return _http_json_response({"granted": grant.public()})

    def _handle_revoke_grant(self, query: dict[str, list[str]]) -> Any:
        node_id = _query_first(query, "nodeId") or ""
        tool = _query_first(query, "tool") or ""
        operator_id = _query_first(query, "operatorId") or ""
        if not self._owned_node(node_id):
            return _http_error(404, "device not found")
        ok = self.authz.revoke(node_id, tool, operator_id)
        if not ok:
            return _http_error(404, "grant not found")
        return _http_json_response({"revoked": {"nodeId": node_id, "tool": tool, "operatorId": operator_id}})

    def _handle_list_requests(self, query: dict[str, list[str]]) -> Any:
        node_id = _query_first(query, "nodeId")
        requests = self.authz.list_requests(node_id=node_id)
        # only surface requests for devices this owner owns
        owned = {d.node_id for d in self.devices.list_devices(owner_id=self._owner_id)}
        return _http_json_response({
            "requests": [r.public() for r in requests if r.node_id in owned]
        })

    def _handle_deny_request(self, query: dict[str, list[str]]) -> Any:
        node_id = _query_first(query, "nodeId") or ""
        operator_id = _query_first(query, "operatorId") or ""
        if not self._owned_node(node_id):
            return _http_error(404, "device not found")
        ok = self.authz.deny_request(node_id, operator_id)
        if not ok:
            return _http_error(404, "request not found")
        return _http_json_response({"denied": {"nodeId": node_id, "operatorId": operator_id}})

    def _handle_list_approvals(self) -> Any:
        return _http_json_response({"approvals": self.broker.list_pending()})

    async def _handle_approve(self, query: dict[str, list[str]]) -> Any:
        approval_id = _query_first(query, "approvalId") or ""
        decision = (_query_first(query, "decision") or "").lower()
        approved = decision in ("approve", "allow", "yes", "true")
        # The approver must own the device the approval is for. In single-owner
        # deployments this is always the WebUI owner; the check makes the route
        # correct-by-construction once multi-user auth lands.
        node_id = self.broker.pending_node(approval_id)
        if node_id is None:
            return _http_error(404, "approval not found or already resolved")
        if not self._owned_node(node_id):
            return _http_error(403, "not the device owner")
        ok = await self.broker.resolve(approval_id, approved=approved)
        if not ok:
            return _http_error(404, "approval not found or already resolved")
        return _http_json_response({"approvalId": approval_id, "approved": approved})

    async def _handle_desktop_takeover(self, query: dict[str, list[str]]) -> Any:
        session_id = _query_first(query, "sessionId") or ""
        ok = await self.desktop.take_over(session_id, owner_id=self._owner_id)
        if not ok:
            return _http_error(404, "session not found")
        return _http_json_response({"takenOver": session_id})

    def _owned_node_ids(self) -> set[str]:
        return {d.node_id for d in self.devices.list_devices(owner_id=self._owner_id)}

    def _handle_desktop_audit(self, query: dict[str, list[str]]) -> Any:
        session_id = _query_first(query, "sessionId")
        records = read_desktop_audit(
            self.workspace_path, node_ids=self._owned_node_ids(), session_id=session_id, limit=200
        )
        return _http_json_response({"records": records})

    def _handle_desktop_recordings(self) -> Any:
        recordings = list_recordings(self.workspace_path, node_ids=self._owned_node_ids())
        return _http_json_response({"recordings": recordings})

    def _handle_desktop_recording_delete(self, query: dict[str, list[str]]) -> Any:
        session_id = _query_first(query, "sessionId") or ""
        # Only allow deleting a recording that belongs to one of the owner's devices.
        owned = {r["sessionId"] for r in list_recordings(self.workspace_path, node_ids=self._owned_node_ids())}
        if session_id not in owned:
            return _http_error(404, "recording not found")
        if not delete_recording(self.workspace_path, session_id):
            return _http_error(404, "recording not found")
        from nanobot.connector.desktop import audit_desktop

        node_id = next(
            (r["nodeId"] for r in read_desktop_audit(self.workspace_path, session_id=session_id, limit=1)),
            "",
        )
        audit_desktop(
            self.workspace_path, session_id=session_id, node_id=node_id,
            operator_id=self._owner_id, action_type="recording.delete",
            params_summary={}, sensitive=False, confirmed=True, result="ok",
        )
        return _http_json_response({"deleted": session_id})

    async def _handle_mcp_tools(self, query: dict[str, list[str]]) -> Any:
        node_id = _query_first(query, "nodeId") or ""
        if not self._owned_node(node_id):
            return _http_error(404, "device not found")
        try:
            status = await self.hub.mcp_status(
                node_id, timeout=self.config.rpc_timeout_s, owner_id=self._owner_id
            )
        except ConnectorError as exc:
            return _http_json_response({"error": exc.code, "message": exc.message}, status=409)
        return _http_json_response({"nodeId": node_id, **status})

    def _handle_exec_audit(self, query: dict[str, list[str]]) -> Any:
        owned = {d.node_id for d in self.devices.list_devices(owner_id=self._owner_id)}
        node_filter = _query_first(query, "nodeId")
        node_ids = {node_filter} & owned if node_filter else owned
        limit_raw = _query_first(query, "limit")
        limit = int(limit_raw) if limit_raw and limit_raw.isdigit() else 100
        records = read_exec_audit(
            self.workspace_path, node_ids=node_ids, limit=min(limit, 500)
        )
        return _http_json_response({"records": records})
