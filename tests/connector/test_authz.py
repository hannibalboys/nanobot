"""Tests for the connector authorization store (add-connector-local-tools)."""

from __future__ import annotations

import time

from nanobot.connector.authz import AuthorizationStore


def _store(tmp_path):
    return AuthorizationStore(tmp_path / "grants.json")


def test_self_use_always_authorized(tmp_path):
    authz = _store(tmp_path)
    # operator == owner: no grant needed
    assert authz.is_authorized("dev-1", "open_app", operator_id="webui", owner_id="webui")


def test_cross_person_denied_without_grant(tmp_path):
    authz = _store(tmp_path)
    assert not authz.is_authorized("dev-1", "open_app", operator_id="alice", owner_id="webui")


def test_grant_then_authorized(tmp_path):
    authz = _store(tmp_path)
    authz.grant("dev-1", "open_app", "alice", granted_by="webui")
    assert authz.is_authorized("dev-1", "open_app", operator_id="alice", owner_id="webui")
    # a different tool is still not authorized (per-tool granularity)
    assert not authz.is_authorized("dev-1", "other", operator_id="alice", owner_id="webui")


def test_revoke_grant(tmp_path):
    authz = _store(tmp_path)
    authz.grant("dev-1", "open_app", "alice", granted_by="webui")
    assert authz.revoke("dev-1", "open_app", "alice") is True
    assert not authz.is_authorized("dev-1", "open_app", operator_id="alice", owner_id="webui")
    assert authz.revoke("dev-1", "open_app", "alice") is False  # already gone


def test_expired_grant_not_authorized(tmp_path):
    authz = _store(tmp_path)
    authz.grant("dev-1", "open_app", "alice", granted_by="webui", ttl_s=1)
    # force expiry by rewriting expires_at into the past
    g = authz.list_grants(node_id="dev-1")[0]
    g.expires_at = time.time() - 5
    assert not authz.is_authorized("dev-1", "open_app", operator_id="alice", owner_id="webui")


def test_active_operators_view(tmp_path):
    authz = _store(tmp_path)
    authz.grant("dev-1", "open_app", "alice", granted_by="webui")
    authz.grant("dev-1", "run_diag", "alice", granted_by="webui")
    authz.grant("dev-1", "open_app", "bob", granted_by="webui")
    ops = {o["operatorId"]: o["tools"] for o in authz.active_operators("dev-1")}
    assert ops["alice"] == ["open_app", "run_diag"]
    assert ops["bob"] == ["open_app"]


def test_access_request_lifecycle(tmp_path):
    authz = _store(tmp_path)
    authz.request_access("dev-1", "alice", tools=["open_app"], reason="need it")
    assert len(authz.list_requests(node_id="dev-1")) == 1
    # granting clears the matching request
    authz.grant("dev-1", "open_app", "alice", granted_by="webui")
    assert authz.list_requests(node_id="dev-1") == []


def test_deny_request(tmp_path):
    authz = _store(tmp_path)
    authz.request_access("dev-1", "alice", tools=["open_app"])
    assert authz.deny_request("dev-1", "alice") is True
    assert authz.deny_request("dev-1", "alice") is False


def test_persistence_survives_reload(tmp_path):
    authz = _store(tmp_path)
    authz.grant("dev-1", "open_app", "alice", granted_by="webui")
    reloaded = AuthorizationStore(tmp_path / "grants.json")
    assert reloaded.is_authorized("dev-1", "open_app", operator_id="alice", owner_id="webui")
