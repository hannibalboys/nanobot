"""Tests for ConnectorGateway HTTP routes (tasks 2.2, 2.3)."""

from __future__ import annotations

import asyncio
import json

from nanobot.channels.websocket.runtime import WebSocketConfig
from nanobot.config.schema import ConnectorConfig
from nanobot.connector.gateway import ConnectorGateway
from nanobot.connector.hub import ConnectorHub

from .conftest import FakeConnector, duplex


class FakeHeaders(dict):
    def get(self, key, default=None):  # case-insensitive-ish for our needs
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return default


class FakeRequest:
    def __init__(self, path: str, headers: dict | None = None):
        self.path = path
        self.headers = FakeHeaders(headers or {})


class FakeConnection:
    def __init__(self, remote=("1.2.3.4", 5555)):
        self.remote_address = remote

    def respond(self, status, reason):
        return ("respond", status, reason)


def _gateway(tmp_path, *, secret="s3cret"):
    cfg = ConnectorConfig(enabled=True)
    ws = WebSocketConfig(token=secret)
    return ConnectorGateway(
        cfg, workspace_path=tmp_path, ws_config=ws, hub=ConnectorHub()
    )


def _auth_headers(secret="s3cret"):
    return {"Authorization": f"Bearer {secret}"}


def _body(resp):
    return json.loads(resp.body.decode("utf-8"))


async def test_pairing_code_requires_auth(tmp_path):
    gw = _gateway(tmp_path)
    req = FakeRequest("/api/connector/pairing-codes")
    resp = await gw.handle_http(FakeConnection(), req, "/api/connector/pairing-codes")
    assert resp.status_code == 401


async def test_generate_code_and_pair_flow(tmp_path):
    gw = _gateway(tmp_path)
    # generate code (authed)
    req = FakeRequest("/api/connector/pairing-codes", _auth_headers())
    resp = await gw.handle_http(FakeConnection(), req, "/api/connector/pairing-codes")
    assert resp.status_code == 200
    code = _body(resp)["code"]

    # redeem via /connector/pair (no auth)
    pair_req = FakeRequest(
        f"/connector/pair?code={code}&name=PC&platform=windows&fingerprint=fp1"
    )
    pair_resp = await gw.handle_http(FakeConnection(), pair_req, "/connector/pair")
    assert pair_resp.status_code == 200
    payload = _body(pair_resp)
    assert payload["nodeId"].startswith("dev-")
    assert gw.devices.verify_token(payload["token"]) is not None


async def test_pair_bad_code_rejected_and_rate_limited(tmp_path):
    gw = _gateway(tmp_path)
    conn = FakeConnection(remote=("9.9.9.9", 1))
    for _ in range(5):
        resp = await gw.handle_http(
            conn, FakeRequest("/connector/pair?code=WRONGCOD"), "/connector/pair"
        )
        assert resp.status_code == 401
    # 6th attempt locked out
    locked = await gw.handle_http(
        conn, FakeRequest("/connector/pair?code=WRONGCOD"), "/connector/pair"
    )
    assert locked.status_code == 429


async def test_revoke_disconnects_online_node(tmp_path):
    gw = _gateway(tmp_path)
    code, _ = gw.devices.generate_pairing_code("webui")
    node_id, _token = gw.devices.redeem_pairing_code(
        code, name="PC", platform="windows", machine_fingerprint="fp-online"
    )

    server_conn, client_conn = duplex()
    serve_task = asyncio.create_task(
        gw.hub.serve(server_conn, node_id=node_id, owner_id="webui")
    )
    connector = FakeConnector(client_conn, files={})
    connector_task = asyncio.create_task(connector.run())
    for _ in range(100):
        if gw.hub.list_nodes():
            break
        await asyncio.sleep(0.01)

    revoke_resp = await gw.handle_http(
        FakeConnection(),
        FakeRequest(f"/api/connector/revoke?nodeId={node_id}", _auth_headers()),
        "/api/connector/revoke",
    )
    assert revoke_resp.status_code == 200
    assert gw.hub.list_nodes() == []
    assert any("revoked" in raw for raw in server_conn.sent)

    await server_conn.close()
    await asyncio.gather(serve_task, connector_task)


async def test_list_nodes_and_revoke(tmp_path):
    gw = _gateway(tmp_path)
    # pair a device
    code, _ = gw.devices.generate_pairing_code("webui")
    node_id, _token = gw.devices.redeem_pairing_code(
        code, name="PC", platform="windows", machine_fingerprint="fp1"
    )

    list_resp = await gw.handle_http(
        FakeConnection(), FakeRequest("/api/connector/nodes", _auth_headers()),
        "/api/connector/nodes",
    )
    nodes = _body(list_resp)["nodes"]
    assert len(nodes) == 1
    assert nodes[0]["nodeId"] == node_id
    assert nodes[0]["online"] is False

    revoke_resp = await gw.handle_http(
        FakeConnection(),
        FakeRequest(f"/api/connector/revoke?nodeId={node_id}", _auth_headers()),
        "/api/connector/revoke",
    )
    assert revoke_resp.status_code == 200
    assert gw.devices.list_devices() == []


async def test_downloads_requires_auth(tmp_path):
    gw = _gateway(tmp_path)
    req = FakeRequest("/api/connector/downloads")
    resp = await gw.handle_http(FakeConnection(), req, "/api/connector/downloads")
    assert resp.status_code == 401


async def test_downloads_payload(tmp_path):
    gw = _gateway(tmp_path)
    req = FakeRequest("/api/connector/downloads", _auth_headers())
    resp = await gw.handle_http(FakeConnection(), req, "/api/connector/downloads")
    assert resp.status_code == 200
    payload = _body(resp)
    assert payload["version"] == "0.1.0"
    assert len(payload["platforms"]) == 3
    assert "sourceInstall" in payload


def test_route_matching(tmp_path):
    gw = _gateway(tmp_path)
    assert gw.matches_ws_path("/connector/ws")
    assert gw.owns_http_route("/connector/pair")
    assert gw.owns_http_route("/api/connector/nodes")
    assert gw.owns_http_route("/api/connector/downloads")
    assert not gw.owns_http_route("/api/other")


def test_disabled_gateway_owns_nothing(tmp_path):
    cfg = ConnectorConfig(enabled=False)
    ws = WebSocketConfig(token="s")
    gw = ConnectorGateway(cfg, workspace_path=tmp_path, ws_config=ws, hub=ConnectorHub())
    assert not gw.enabled
    assert not gw.matches_ws_path("/connector/ws")
    assert not gw.owns_http_route("/connector/pair")
