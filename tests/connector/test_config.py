"""Tests for ConnectorConfig (task 1.1)."""

import pytest
from pydantic import ValidationError

from nanobot.config.schema import Config, ConnectorConfig


def test_defaults_disabled():
    cfg = ConnectorConfig()
    assert cfg.enabled is False
    assert cfg.path == "/connector/ws"
    assert cfg.allow_exec is False
    assert cfg.max_file_bytes == 209_715_200


def test_exec_defaults_conservative():
    cfg = ConnectorConfig()
    assert cfg.allow_exec is False
    assert cfg.max_concurrent_execs == 2
    assert cfg.exec_timeout_s == 300
    assert cfg.max_exec_output_bytes == 1_048_576
    assert cfg.approval_ttl_s == 120
    assert cfg.exec_rate_per_minute == 30


def test_exec_camelcase_aliases():
    cfg = ConnectorConfig.model_validate(
        {
            "allowExec": True,
            "maxConcurrentExecs": 4,
            "execTimeoutS": 120,
            "maxExecOutputBytes": 4096,
            "approvalTtlS": 60,
            "execRatePerMinute": 10,
        }
    )
    assert cfg.allow_exec is True
    assert cfg.max_concurrent_execs == 4
    assert cfg.exec_timeout_s == 120
    assert cfg.max_exec_output_bytes == 4096
    assert cfg.approval_ttl_s == 60
    assert cfg.exec_rate_per_minute == 10
    # round-trips back to camelCase
    dumped = cfg.model_dump(by_alias=True)
    assert dumped["maxConcurrentExecs"] == 4
    assert dumped["execRatePerMinute"] == 10


def test_mcp_proxy_default_off_and_alias():
    assert ConnectorConfig().allow_mcp_proxy is False
    cfg = ConnectorConfig.model_validate({"allowMcpProxy": True})
    assert cfg.allow_mcp_proxy is True
    assert cfg.model_dump(by_alias=True)["allowMcpProxy"] is True


def test_desktop_control_defaults_and_aliases():
    cfg = ConnectorConfig()
    assert cfg.allow_desktop_control is False
    assert cfg.desktop_max_fps == 2
    assert cfg.desktop_max_dimension == 1280
    assert cfg.desktop_session_max_s == 900
    assert cfg.desktop_idle_timeout_s == 120
    assert cfg.desktop_recording_retention_days == 7

    cfg2 = ConnectorConfig.model_validate({
        "allowDesktopControl": True,
        "desktopMaxFps": 5,
        "desktopMaxDimension": 1920,
        "desktopSessionMaxS": 600,
        "desktopIdleTimeoutS": 60,
        "desktopRecordingRetentionDays": 3,
    })
    assert cfg2.allow_desktop_control is True
    assert cfg2.desktop_max_fps == 5
    assert cfg2.desktop_max_dimension == 1920
    assert cfg2.desktop_session_max_s == 600
    assert cfg2.desktop_idle_timeout_s == 60
    assert cfg2.desktop_recording_retention_days == 3
    assert cfg2.model_dump(by_alias=True)["desktopMaxFps"] == 5


def test_camelcase_aliases():
    cfg = ConnectorConfig.model_validate(
        {
            "enabled": True,
            "pairingCodeTtlS": 300,
            "transferTimeoutS": 900,
            "fetchCacheMaxBytes": 1024,
        }
    )
    assert cfg.enabled is True
    assert cfg.pairing_code_ttl_s == 300
    assert cfg.transfer_timeout_s == 900
    assert cfg.fetch_cache_max_bytes == 1024


def test_path_must_start_with_slash():
    with pytest.raises(ValidationError):
        ConnectorConfig(path="connector/ws")


def test_present_on_root_config_and_defaults_off():
    cfg = Config()
    assert cfg.connector.enabled is False
    # camelCase round-trips through the root config
    dumped = cfg.model_dump(by_alias=True)
    assert dumped["connector"]["path"] == "/connector/ws"
