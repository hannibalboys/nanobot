"""Tests for the on-device arm store + daemon consent wiring."""

from __future__ import annotations

import pytest

from nanobot_connector.arm import ArmStore, parse_duration
from nanobot_connector.client import build_daemon_client
from nanobot_connector.config import ConnectorClientConfig


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("NANOBOT_CONNECTOR_HOME", str(tmp_path / "home"))


def test_parse_duration():
    assert parse_duration("30m") == 1800
    assert parse_duration("2h") == 7200
    assert parse_duration("90s") == 90
    assert parse_duration("45") == 45
    with pytest.raises(ValueError):
        parse_duration("")


def test_arm_and_expiry(tmp_path):
    store = ArmStore(tmp_path / "arm.json")
    assert store.is_armed("desktop") is False
    store.arm("desktop", 60)
    assert store.is_armed("desktop") is True
    assert "desktop" in store.status()
    # force expiry
    data = store._load()
    data["desktop"] = 1.0  # far past
    store._save(data)
    assert store.is_armed("desktop") is False
    assert store.status() == {}


def test_disarm(tmp_path):
    store = ArmStore(tmp_path / "arm.json")
    store.arm("exec", 60)
    store.arm("mcp", 60)
    store.disarm("exec")
    assert store.is_armed("exec") is False
    assert store.is_armed("mcp") is True
    store.disarm("all")
    assert store.is_armed("mcp") is False


def test_unknown_category_rejected(tmp_path):
    with pytest.raises(ValueError):
        ArmStore(tmp_path / "arm.json").arm("bogus", 60)


async def test_daemon_client_local_approval_follows_arm(tmp_path, monkeypatch):
    # arm store shares the isolated home
    store = ArmStore()
    cfg = ConnectorClientConfig(server="wss://h/connector/ws", device_token="t")
    client = build_daemon_client(cfg)

    # exec local approval is gated by the arm store
    assert await client._on_local_approval(object(), {}) is False
    store.arm("exec", 60)
    assert await client._on_local_approval(object(), {}) is True
    store.disarm("exec")
    assert await client._on_local_approval(object(), {}) is False


async def test_daemon_desktop_only_when_enabled(tmp_path):
    cfg = ConnectorClientConfig(server="wss://h/c", device_token="t", desktop_enabled=False)
    assert build_daemon_client(cfg).desktop is None

    cfg2 = ConnectorClientConfig(server="wss://h/c", device_token="t", desktop_enabled=True)
    client = build_daemon_client(cfg2)
    assert client.desktop is not None
    # desktop authorize hook is wired to the arm store
    assert await client.desktop.on_local_authorize("webui", "goal") is False
    ArmStore().arm("desktop", 60)
    assert await client.desktop.on_local_authorize("webui", "goal") is True
