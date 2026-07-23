"""Safe lifecycle helpers for portable nanobot configuration."""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

from nanobot.config.loader import (
    _migrate_config,
    _resolve_env_vars,
    _write_json_atomic,
    config_to_dict,
    load_config,
    save_config,
)
from nanobot.config.schema import Config

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_FORBIDDEN_TEMPLATE_KEYS = {
    "devicetoken",
    "nodeid",
    "pairingcode",
    "grants",
    "devices",
    "audit",
    "secrets",
}
_SENSITIVE_VALUE_KEYS = {"apikey", "token", "secret", "password", "privatekey"}


class ConfigBootstrapError(ValueError):
    """A safe, user-facing configuration lifecycle error."""


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


def builtin_template_path() -> Path:
    return Path(__file__).with_name("templates") / "config.example.json"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigBootstrapError(f"无法读取配置文件 {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigBootstrapError(f"配置文件不是有效 JSON: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigBootstrapError(f"配置文件根节点必须是对象: {path}")
    return data


def _walk(value: Any, path: str = ""):
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            yield child_path, str(key), child
            yield from _walk(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk(child, f"{path}[{index}]")


def _is_env_reference(value: object) -> bool:
    return isinstance(value, str) and bool(_ENV_REF.fullmatch(value.strip()))


def _is_sensitive_key(key: str) -> bool:
    normalized = key.replace("_", "").lower()
    return any(marker in normalized for marker in _SENSITIVE_VALUE_KEYS)


def _validate_template(data: dict[str, Any]) -> None:
    errors: list[str] = []
    for field_path, key, value in _walk(data):
        normalized = key.replace("_", "").lower()
        if normalized in _FORBIDDEN_TEMPLATE_KEYS:
            errors.append(f"模板禁止包含 {field_path}")
        if _is_sensitive_key(key) and isinstance(value, str):
            if value.strip() and not _is_env_reference(value):
                errors.append(f"模板中的敏感字段必须为空或环境变量引用：{field_path}")
    if errors:
        raise ConfigBootstrapError("；".join(errors))


def _merge(base: Any, overlay: Any) -> Any:
    if not isinstance(base, dict) or not isinstance(overlay, dict):
        return overlay
    merged = dict(base)
    for key, value in overlay.items():
        merged[key] = _merge(merged[key], value) if key in merged else value
    return merged


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.{stamp}.bak")
    shutil.copy2(path, backup)
    try:
        os.chmod(backup, 0o600)
    except OSError:
        pass
    return backup


def _merge_plugin_defaults(path: Path) -> None:
    """Add discovered channel defaults without importing channel runtimes."""
    from nanobot.channels.contracts import channel_default_config
    from nanobot.channels.registry import discover_plugins
    from nanobot.config.loader import merge_missing_defaults

    plugins = discover_plugins()
    if not plugins:
        return
    try:
        with FileLock(str(path) + ".lock", timeout=10):
            data = _read_json(path)
            channels = data.setdefault("channels", {})
            if not isinstance(channels, dict):
                raise ConfigBootstrapError("channels 必须是对象")
            changed = False
            for name, plugin in plugins.items():
                defaults = channel_default_config(plugin)
                existing = channels.get(name)
                merged = defaults if existing is None else merge_missing_defaults(existing, defaults)
                if merged != existing:
                    channels[name] = merged
                    changed = True
            if changed:
                _write_json_atomic(path, data)
    except Timeout as exc:
        raise ConfigBootstrapError(f"等待插件配置写入锁超时：{path}") from exc


def initialize_config(
    path: Path,
    *,
    template_path: Path | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> tuple[Config, Path | None]:
    """Create a complete config from defaults and an optional safe template."""
    path = path.expanduser().resolve()
    if path.exists() and not force:
        raise ConfigBootstrapError(f"配置已存在：{path}（如确需覆盖，请显式传入 --force）")

    template = _read_json((template_path or builtin_template_path()).expanduser().resolve())
    _validate_template(template)
    defaults = config_to_dict(Config())
    merged = _merge(defaults, template)
    try:
        config = Config.model_validate(_migrate_config(merged))
    except ValueError as exc:
        raise ConfigBootstrapError(f"模板不符合配置 Schema: {exc}") from exc
    backup = _backup(path) if path.exists() and not dry_run else None
    if not dry_run:
        save_config(config, path)
        _merge_plugin_defaults(path)
    return config, backup


def refresh_config(path: Path, *, dry_run: bool = False) -> tuple[Config, Path | None]:
    """Migrate an existing config and serialize any newly-added defaults."""
    path = path.expanduser().resolve()
    if not path.exists():
        raise ConfigBootstrapError(f"配置不存在：{path}")
    try:
        config = load_config(path)
    except ValueError as exc:
        raise ConfigBootstrapError(str(exc)) from exc
    backup = _backup(path) if not dry_run else None
    if not dry_run:
        save_config(config, path)
        _merge_plugin_defaults(path)
    return config, backup


def _environment_references(data: Any) -> list[str]:
    refs: set[str] = set()
    for _path, _key, value in _walk(data):
        if isinstance(value, str):
            refs.update(_ENV_REF.findall(value))
    return sorted(refs)


def _inline_secret_fields(data: dict[str, Any]) -> list[str]:
    fields: list[str] = []
    for field_path, key, value in _walk(data):
        if _is_sensitive_key(key) and isinstance(value, str):
            if value.strip() and not _is_env_reference(value):
                fields.append(field_path)
    return fields


def validate_config(path: Path, *, strict: bool = False) -> CheckResult:
    """Validate a config without writing it or displaying secret values."""
    path = path.expanduser().resolve()
    errors: list[str] = []
    warnings: list[str] = []
    try:
        raw = _read_json(path)
        migrated = _migrate_config(raw)
        Config.model_validate(migrated)
    except (ConfigBootstrapError, ValueError) as exc:
        return CheckResult(ok=False, errors=[str(exc)])

    refs = _environment_references(raw)
    missing = [name for name in refs if os.environ.get(name) is None]
    if missing:
        target = errors if strict else warnings
        target.append(f"未设置环境变量：{', '.join(missing)}")
    if strict and not missing:
        try:
            Config.model_validate(_migrate_config(_resolve_env_vars(raw)))
        except ValueError as exc:
            errors.append(f"环境变量解析后的配置无效：{exc}")

    inline = _inline_secret_fields(raw)
    if inline:
        target = errors if strict else warnings
        target.append(f"检测到内联敏感字段：{', '.join(inline)}")
    return CheckResult(
        ok=not errors,
        errors=errors,
        warnings=warnings,
        details={"path": str(path), "environmentReferences": refs},
    )


def doctor_config(path: Path, *, strict: bool = False) -> CheckResult:
    """Report deployment readiness without modifying configuration or state."""
    result = validate_config(path, strict=strict)
    warnings = list(result.warnings)
    errors = list(result.errors)
    details = dict(result.details)
    if not path.exists():
        errors.append(f"配置不存在：{path}")
        return CheckResult(False, errors, warnings, details)
    if not os.access(path.parent, os.W_OK):
        errors.append(f"配置目录不可写：{path.parent}")
    if os.name != "nt":
        try:
            if path.stat().st_mode & 0o077:
                message = "配置文件权限过宽，建议仅允许当前用户读取"
                (errors if strict else warnings).append(message)
        except OSError as exc:
            warnings.append(f"无法检查配置文件权限：{exc}")
    try:
        config = load_config(path)
        workspace = config.workspace_path
        details["workspace"] = str(workspace)
        if not workspace.exists():
            warnings.append(f"工作区尚不存在，初始化后将创建：{workspace}")
        if config.connector.allow_exec or config.connector.allow_desktop_control:
            warnings.append("连接器高风险能力已开启，请确认设备授权、审计和最小权限策略")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(0.1)
            if sock.connect_ex(("127.0.0.1", config.gateway.port)) == 0:
                warnings.append(f"网关端口 {config.gateway.port} 当前正在使用（仅为建议性检查）")
        finally:
            sock.close()
    except ValueError:
        pass
    return CheckResult(ok=not errors, errors=errors, warnings=warnings, details=details)


def export_profile(source: Path, output: Path, *, dry_run: bool = False) -> None:
    """Export a reviewable deployment profile without inline secrets."""
    config = load_config(source.expanduser().resolve())
    data = config_to_dict(config)

    def sanitize(value: Any, key: str = "") -> Any:
        normalized = key.replace("_", "").lower()
        if normalized in _FORBIDDEN_TEMPLATE_KEYS:
            return None
        if _is_sensitive_key(key) and isinstance(value, str) and value.strip():
            return ""
        if isinstance(value, dict):
            result: dict[str, Any] = {}
            for child_key, child_value in value.items():
                child = sanitize(child_value, child_key)
                if child is not None:
                    result[child_key] = child
            return result
        if isinstance(value, list):
            return [sanitize(v) for v in value]
        return value

    sanitized = sanitize(data)
    if not isinstance(sanitized, dict):  # defensive, Config always serializes to a dict
        raise ConfigBootstrapError("无法导出部署档案")
    if not dry_run:
        _write_json_atomic(output.expanduser().resolve(), sanitized)
