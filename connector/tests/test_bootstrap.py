from __future__ import annotations

import json
import sys

import pytest

from nanobot_connector.bootstrap import (
    ConnectorBootstrapError,
    doctor_connector,
    export_tool_template,
    import_template,
    initialize_connector,
)
from nanobot_connector.config import ConnectorClientConfig, config_dir, config_path
from nanobot_connector.persistence import file_permission_issue
from nanobot_connector.tools import ToolDef, ToolRegistry


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("NANOBOT_CONNECTOR_HOME", str(tmp_path / "home"))


def test_init_creates_safe_unpaired_state() -> None:
    cfg = initialize_connector()

    assert config_path().exists()
    assert (config_dir() / "tools.json").exists()
    assert cfg.device_token == ""
    assert cfg.roots == []
    assert cfg.desktop_enabled is False
    assert file_permission_issue(config_path()) is None
    assert file_permission_issue(config_dir() / "tools.json") is None


def test_import_template_preflights_then_writes_atomically(tmp_path, monkeypatch) -> None:
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "safe.json").write_text(
        json.dumps(
            {
                "tools": [
                    {
                        "name": "python-version",
                        "exec": sys.executable,
                        "argv": ["--version"],
                        "approval": "local",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("nanobot_connector.bootstrap.templates_dir", lambda: templates)

    assert import_template("safe") == ["python-version"]
    assert [tool.name for tool in ToolRegistry.load().list()] == ["python-version"]
    with pytest.raises(ConnectorBootstrapError, match="已存在同名工具"):
        import_template("safe")


def test_import_template_does_not_write_when_executable_is_missing(tmp_path, monkeypatch) -> None:
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "missing.json").write_text(
        json.dumps({"tools": [{"name": "missing", "exec": "definitely-not-a-program"}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr("nanobot_connector.bootstrap.templates_dir", lambda: templates)

    with pytest.raises(ConnectorBootstrapError, match="缺少可执行文件"):
        import_template("missing")
    assert ToolRegistry.load().list() == []


def test_export_refuses_machine_specific_executable(tmp_path) -> None:
    ToolRegistry([ToolDef(name="local", exec=str(tmp_path / "local.exe"))]).save()

    with pytest.raises(ConnectorBootstrapError, match="绝对可执行文件路径"):
        export_tool_template("local", tmp_path / "tool.json")


def test_strict_doctor_rejects_insecure_transport() -> None:
    ConnectorClientConfig(server="wss://example.test", device_token="token", insecure=True).save()

    result = doctor_connector(strict=True)

    assert not result.ok
    assert any("insecure" in message for message in result.errors)


def test_doctor_warns_when_a_persistent_gui_waits_for_exit() -> None:
    ToolRegistry([ToolDef(name="qq", exec="QQ.exe", completion="wait")]).save()

    result = doctor_connector()

    assert any("completion=wait" in message and "qq" in message for message in result.warnings)
