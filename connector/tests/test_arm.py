"""Tests for the on-device arm store + daemon consent wiring."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from nanobot_connector.arm import ArmStore, parse_duration
from nanobot_connector.cli import app
from nanobot_connector.client import build_daemon_client
from nanobot_connector.config import ConnectorClientConfig
from nanobot_connector.persistence import LocalStateError


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


def test_remaining(tmp_path):
    store = ArmStore(tmp_path / "arm.json")
    assert store.remaining("exec") == 0
    store.arm("exec", 120)
    assert 0 < store.remaining("exec") <= 120
    # force expiry
    data = store._load()
    data["exec"] = 1.0  # far past
    store._save(data)
    assert store.remaining("exec") == 0


def test_remaining_rounds_up_a_still_valid_subsecond_window(tmp_path, monkeypatch):
    store = ArmStore(tmp_path / "arm.json")
    now = 1_000.0
    monkeypatch.setattr("nanobot_connector.arm.time.time", lambda: now)
    store._save({"exec": now + 0.01})

    assert store.is_armed("exec") is True
    assert store.remaining("exec") == 1
    assert store.status() == {"exec": 1}


def test_malformed_arm_state_fails_closed(tmp_path):
    path = tmp_path / "arm.json"
    path.write_text("{", encoding="utf-8")

    with pytest.raises(LocalStateError, match="有效 JSON"):
        ArmStore(path).is_armed("exec")


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

    # the live arm window is exposed for tools.list/mcp.list annotation
    assert client._armed_remaining is not None
    assert client._armed_remaining("exec") == 0
    store.arm("exec", 60)
    assert 0 < client._armed_remaining("exec") <= 60


async def test_daemon_client_fails_closed_when_arm_state_is_corrupt():
    path = ArmStore()._path
    path.parent.mkdir(parents=True)
    path.write_text("{", encoding="utf-8")
    client = build_daemon_client(ConnectorClientConfig(server="wss://h/c", device_token="t"))

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


# -- CLI dispatch (the optional positional must not swallow subcommands) ------


def test_cli_arm_status_and_disarm_roundtrip():
    runner = CliRunner()
    ArmStore().arm("exec", 600)

    result = runner.invoke(app, ["arm", "status"])
    assert result.exit_code == 0
    assert "exec" in result.output

    result = runner.invoke(app, ["arm", "disarm", "exec"])
    assert result.exit_code == 0
    assert ArmStore().is_armed("exec") is False

    result = runner.invoke(app, ["arm", "disarm"])
    assert result.exit_code == 0


def test_cli_arm_category_with_duration():
    runner = CliRunner()
    result = runner.invoke(app, ["arm", "desktop", "--for", "10m"])
    assert result.exit_code == 0
    assert ArmStore().is_armed("desktop") is True


def test_cli_arm_rejects_unknown_category_and_extras():
    runner = CliRunner()
    assert runner.invoke(app, ["arm", "bogus"]).exit_code == 1
    assert runner.invoke(app, ["arm", "exec", "extra"]).exit_code == 1
    assert runner.invoke(app, ["arm", "disarm", "bogus"]).exit_code == 1


def test_cli_arm_status_reports_corrupt_state_without_traceback():
    path = ArmStore()._path
    path.parent.mkdir(parents=True)
    path.write_text("{", encoding="utf-8")

    result = CliRunner().invoke(app, ["arm", "status"])

    assert result.exit_code == 1
    assert "无法读取本机授权状态" in result.output
