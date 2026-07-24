from __future__ import annotations

import json

from typer.testing import CliRunner

from nanobot.cli.commands import app


def test_config_init_validate_and_refresh_with_explicit_paths(tmp_path, monkeypatch) -> None:
    template = tmp_path / "profile.json"
    workspace = tmp_path / "workspace"
    template.write_text(
        json.dumps({"agents": {"defaults": {"workspace": str(workspace)}}}),
        encoding="utf-8",
    )
    target = tmp_path / "config.json"
    monkeypatch.setattr("nanobot.cli.config_commands.sync_workspace_templates", lambda *_args, **_kw: [])
    runner = CliRunner()

    initialized = runner.invoke(
        app,
        ["config", "init", "--config", str(target), "--template", str(template), "--json"],
    )
    validated = runner.invoke(app, ["config", "validate", "--config", str(target), "--strict"])
    refreshed = runner.invoke(app, ["config", "refresh", "--config", str(target)])
    exported = runner.invoke(
        app,
        ["config", "export-profile", "--config", str(target), "--output", str(tmp_path / "out.json"), "--json"],
    )

    assert initialized.exit_code == 0, initialized.output
    assert json.loads(initialized.output)["ok"] is True
    assert validated.exit_code == 0, validated.output
    assert refreshed.exit_code == 0, refreshed.output
    assert "请重启 gateway" in refreshed.output
    assert exported.exit_code == 0, exported.output
    assert json.loads(exported.output)["ok"] is True
