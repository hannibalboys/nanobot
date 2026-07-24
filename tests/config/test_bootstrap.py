from __future__ import annotations

import json

import pytest

from nanobot.config.bootstrap import (
    ConfigBootstrapError,
    export_profile,
    initialize_config,
    refresh_config,
    validate_config,
)
from nanobot.config.loader import file_permission_issue
from nanobot.config.schema import CONFIG_SCHEMA_VERSION


def test_initialize_creates_versioned_config_from_safe_template(tmp_path) -> None:
    template = tmp_path / "profile.json"
    template.write_text(
        json.dumps({"connector": {"enabled": True, "allowExec": False}}),
        encoding="utf-8",
    )
    target = tmp_path / "config.json"

    config, backup = initialize_config(target, template_path=template)

    raw = json.loads(target.read_text(encoding="utf-8"))
    assert backup is None
    assert config.schema_version == CONFIG_SCHEMA_VERSION
    assert raw["schemaVersion"] == CONFIG_SCHEMA_VERSION
    assert raw["connector"]["enabled"] is True
    assert raw["connector"]["allowExec"] is False
    assert file_permission_issue(target) is None


def test_initialize_merges_builtin_template_before_explicit_profile(tmp_path) -> None:
    template = tmp_path / "profile.json"
    template.write_text(json.dumps({"connector": {"enabled": True}}), encoding="utf-8")
    target = tmp_path / "config.json"

    initialize_config(target, template_path=template)

    raw = json.loads(target.read_text(encoding="utf-8"))
    assert raw["agents"]["defaults"]["workspace"] == "~/.nanobot/workspace"
    assert raw["connector"]["enabled"] is True
    assert raw["connector"]["allowExec"] is False
    assert raw["connector"]["allowMcpProxy"] is False
    assert raw["connector"]["allowDesktopControl"] is False


def test_initialize_refuses_existing_config_without_force(tmp_path) -> None:
    target = tmp_path / "config.json"
    target.write_text("{}", encoding="utf-8")

    with pytest.raises(ConfigBootstrapError, match="配置已存在"):
        initialize_config(target)


def test_initialize_force_keeps_a_private_backup(tmp_path) -> None:
    target = tmp_path / "config.json"
    target.write_text(json.dumps({"tools": {"myEnabled": False}}), encoding="utf-8")

    _config, backup = initialize_config(target, force=True)

    assert backup is not None and backup.exists()
    assert json.loads(backup.read_text(encoding="utf-8"))["tools"]["myEnabled"] is False


def test_initialize_rejects_device_identity_and_inline_secrets_in_template(tmp_path) -> None:
    template = tmp_path / "unsafe.json"
    template.write_text(
        json.dumps({"deviceToken": "token", "providers": {"groq": {"apiKey": "secret"}}}),
        encoding="utf-8",
    )

    with pytest.raises(ConfigBootstrapError, match="模板禁止包含"):
        initialize_config(tmp_path / "config.json", template_path=template)


def test_refresh_migrates_legacy_config_and_creates_backup(tmp_path) -> None:
    target = tmp_path / "config.json"
    target.write_text(json.dumps({"tools": {"myEnabled": False}}), encoding="utf-8")

    config, backup = refresh_config(target)

    raw = json.loads(target.read_text(encoding="utf-8"))
    assert backup is not None and backup.exists()
    assert config.tools.my.enable is False
    assert raw["schemaVersion"] == CONFIG_SCHEMA_VERSION
    assert raw["tools"]["my"]["enable"] is False


def test_refresh_write_failure_keeps_original_config(tmp_path, monkeypatch) -> None:
    target = tmp_path / "config.json"
    original = json.dumps({"tools": {"myEnabled": False}})
    target.write_text(original, encoding="utf-8")

    def fail_write(*_args, **_kwargs) -> None:
        raise OSError("simulated disk failure")

    monkeypatch.setattr("nanobot.config.bootstrap._write_json_atomic", fail_write)
    with pytest.raises(ConfigBootstrapError, match="无法安全刷新"):
        refresh_config(target)

    assert target.read_text(encoding="utf-8") == original


def test_validate_strict_checks_environment_without_printing_value(tmp_path) -> None:
    target = tmp_path / "config.json"
    target.write_text(
        json.dumps({"providers": {"groq": {"apiKey": "${MISSING_FOR_TEST}"}}}),
        encoding="utf-8",
    )

    result = validate_config(target, strict=True)

    assert not result.ok
    assert "MISSING_FOR_TEST" in result.errors[0]


def test_export_profile_redacts_inline_secrets(tmp_path) -> None:
    source = tmp_path / "config.json"
    source.write_text(
        json.dumps(
            {
                "providers": {"groq": {"apiKey": "private-value"}},
                "channels": {"websocket": {"tokenIssueSecret": "another-private-value"}},
                "agents": {"defaults": {"workspace": str(tmp_path / "private-workspace")}},
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "profile.json"

    export_profile(source, output)

    exported = json.loads(output.read_text(encoding="utf-8"))
    assert exported["providers"]["groq"]["apiKey"] == ""
    assert exported["channels"]["websocket"]["tokenIssueSecret"] == ""
    assert exported["agents"]["defaults"]["workspace"] == "~/.nanobot/workspace"
    assert exported["profileReviewRequired"] is True
    assert "private-value" not in output.read_text(encoding="utf-8")
