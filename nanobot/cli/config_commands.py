"""CLI adapters for portable configuration lifecycle operations."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer

from nanobot.config.bootstrap import (
    ConfigBootstrapError,
    doctor_config,
    export_profile,
    initialize_config,
    refresh_config,
    validate_config,
)
from nanobot.config.loader import get_config_path, set_config_path
from nanobot.config.paths import get_workspace_path
from nanobot.utils.helpers import sync_workspace_templates


def _target_path(value: str | None) -> Path:
    path = Path(value).expanduser().resolve() if value else get_config_path()
    set_config_path(path)
    return path


def _emit(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        typer.echo(json.dumps(payload, ensure_ascii=False))
        return
    for message in payload.get("errors", []):
        typer.secho(f"错误：{message}", fg=typer.colors.RED)
    for message in payload.get("warnings", []):
        typer.secho(f"提示：{message}", fg=typer.colors.YELLOW)
    if payload.get("ok"):
        typer.secho(payload.get("message", "检查通过"), fg=typer.colors.GREEN)


def create_config_app() -> typer.Typer:
    app = typer.Typer(help="初始化、升级、校验和诊断 nanobot 配置。")

    @app.command("init")
    def init(
        config: str | None = typer.Option(None, "--config", "-c", help="目标配置文件路径。"),
        template: str | None = typer.Option(None, "--template", "-t", help="无密钥部署档案路径。"),
        force: bool = typer.Option(False, "--force", help="备份后覆盖已有配置。"),
        dry_run: bool = typer.Option(False, "--dry-run", help="仅校验，不写入文件。"),
        json_output: bool = typer.Option(False, "--json", help="输出机器可读 JSON。"),
    ) -> None:
        """从安全模板创建完整配置，不会创建设备或授权状态。"""
        path = _target_path(config)
        try:
            loaded, backup = initialize_config(
                path,
                template_path=Path(template) if template else None,
                force=force,
                dry_run=dry_run,
            )
            if not dry_run:
                workspace = get_workspace_path(loaded.workspace_path)
                sync_workspace_templates(workspace, silent=True)
            _emit(
                {
                    "ok": True,
                    "message": "配置已生成；请注入密钥后执行 config validate --strict。",
                    "path": str(path),
                    "backup": str(backup) if backup else None,
                    "dryRun": dry_run,
                },
                as_json=json_output,
            )
        except ConfigBootstrapError as exc:
            _emit({"ok": False, "errors": [str(exc)]}, as_json=json_output)
            raise typer.Exit(1) from exc

    @app.command("refresh")
    def refresh(
        config: str | None = typer.Option(None, "--config", "-c", help="配置文件路径。"),
        dry_run: bool = typer.Option(False, "--dry-run", help="仅预览，不写入文件。"),
        json_output: bool = typer.Option(False, "--json", help="输出机器可读 JSON。"),
    ) -> None:
        """迁移旧配置并补齐当前默认字段；成功后需重启网关。"""
        path = _target_path(config)
        try:
            _config, backup = refresh_config(path, dry_run=dry_run)
            _emit(
                {
                    "ok": True,
                    "message": "配置已刷新；运行中的网关不会热更新，请重启 gateway。",
                    "path": str(path),
                    "backup": str(backup) if backup else None,
                    "dryRun": dry_run,
                },
                as_json=json_output,
            )
        except ConfigBootstrapError as exc:
            _emit({"ok": False, "errors": [str(exc)]}, as_json=json_output)
            raise typer.Exit(1) from exc

    @app.command("validate")
    def validate(
        config: str | None = typer.Option(None, "--config", "-c", help="配置文件路径。"),
        strict: bool = typer.Option(False, "--strict", help="同时检查环境变量和内联敏感值。"),
        json_output: bool = typer.Option(False, "--json", help="输出机器可读 JSON。"),
    ) -> None:
        """只读校验配置。"""
        result = validate_config(_target_path(config), strict=strict)
        _emit({"ok": result.ok, **result.__dict__}, as_json=json_output)
        if not result.ok:
            raise typer.Exit(1)

    @app.command("doctor")
    def doctor(
        config: str | None = typer.Option(None, "--config", "-c", help="配置文件路径。"),
        strict: bool = typer.Option(False, "--strict", help="把权限和秘密问题视为错误。"),
        json_output: bool = typer.Option(False, "--json", help="输出机器可读 JSON。"),
    ) -> None:
        """只读诊断部署环境。"""
        result = doctor_config(_target_path(config), strict=strict)
        _emit({"ok": result.ok, **result.__dict__}, as_json=json_output)
        if not result.ok:
            raise typer.Exit(1)

    @app.command("export-profile")
    def export_profile_command(
        output: str = typer.Option(..., "--output", "-o", help="输出的无密钥部署档案路径。"),
        config: str | None = typer.Option(None, "--config", "-c", help="源配置文件路径。"),
        dry_run: bool = typer.Option(False, "--dry-run", help="仅检查，不写入文件。"),
        json_output: bool = typer.Option(False, "--json", help="输出机器可读 JSON。"),
    ) -> None:
        """从现有配置导出需人工审阅的无密钥部署档案。"""
        try:
            export_profile(_target_path(config), Path(output), dry_run=dry_run)
            _emit(
                {
                    "ok": True,
                    "message": "已导出无密钥部署档案，请在提交前人工审阅。",
                    "output": str(Path(output).expanduser().resolve()),
                    "dryRun": dry_run,
                },
                as_json=json_output,
            )
        except ConfigBootstrapError as exc:
            _emit({"ok": False, "errors": [str(exc)]}, as_json=json_output)
            raise typer.Exit(1) from exc

    return app
