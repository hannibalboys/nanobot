"""Tests for connector client config and client helpers (task 5.7)."""

from __future__ import annotations

import pytest

from nanobot_connector.client import ConnectorClient, machine_fingerprint
from nanobot_connector.config import ConnectorClientConfig, config_path


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("NANOBOT_CONNECTOR_HOME", str(tmp_path / "home"))


def test_config_save_load_roundtrip():
    cfg = ConnectorClientConfig(
        server="wss://192.168.90.100:8765",
        device_token="tok",
        node_id="dev-1",
        roots=["D:/PPT"],
        cert_fingerprint="abc",
    )
    cfg.save()
    assert config_path().exists()
    loaded = ConnectorClientConfig.load()
    assert loaded.server == "wss://192.168.90.100:8765"
    assert loaded.roots == ["D:/PPT"]
    assert loaded.cert_fingerprint == "abc"


def test_config_camelcase_on_disk():
    ConnectorClientConfig(device_token="t", cert_fingerprint="fp").save()
    raw = config_path().read_text(encoding="utf-8")
    assert "deviceToken" in raw
    assert "certFingerprint" in raw


def test_machine_fingerprint_stable():
    assert machine_fingerprint() == machine_fingerprint()
    assert len(machine_fingerprint()) == 32


def test_ws_url_includes_token():
    cfg = ConnectorClientConfig(server="wss://host:8765/connector/ws", device_token="secret")
    client = ConnectorClient(cfg)
    url, scheme = client._ws_url()
    assert scheme == "wss"
    assert "device_token=secret" in url


def test_ws_url_default_path():
    cfg = ConnectorClientConfig(server="wss://host:8765", device_token="s")
    url, _ = ConnectorClient(cfg)._ws_url()
    assert "/connector/ws" in url
