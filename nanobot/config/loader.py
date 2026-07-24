"""Configuration loading utilities."""

import json
import os
import re
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any

import pydantic
from filelock import FileLock, Timeout
from loguru import logger
from pydantic import BaseModel

from nanobot.config.schema import CONFIG_SCHEMA_VERSION, Config, _resolve_tool_config_refs

# Global variable to store current config path (for multi-instance support)
_current_config_path: Path | None = None
_schema_refs_ready = False


def restrict_file_permissions(path: Path) -> None:
    """Limit a declaration or backup file to its owner on supported platforms."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        # Windows chmod only controls a read-only attribute; its DACL is set below.
        pass
    if os.name != "nt":
        return
    try:
        import ntsecuritycon
        import win32api
        import win32security

        user_sid = win32security.LookupAccountName(None, win32api.GetUserName())[0]
        system_sid = win32security.CreateWellKnownSid(win32security.WinLocalSystemSid, None)
        admins_sid = win32security.CreateWellKnownSid(win32security.WinBuiltinAdministratorsSid, None)
        dacl = win32security.ACL()
        user_access = ntsecuritycon.FILE_GENERIC_READ | ntsecuritycon.FILE_GENERIC_WRITE
        dacl.AddAccessAllowedAce(win32security.ACL_REVISION, user_access, user_sid)
        dacl.AddAccessAllowedAce(win32security.ACL_REVISION, ntsecuritycon.FILE_ALL_ACCESS, system_sid)
        dacl.AddAccessAllowedAce(win32security.ACL_REVISION, ntsecuritycon.FILE_ALL_ACCESS, admins_sid)
        security_info = (
            win32security.DACL_SECURITY_INFORMATION
            | win32security.PROTECTED_DACL_SECURITY_INFORMATION
        )
        win32security.SetNamedSecurityInfo(
            str(path), win32security.SE_FILE_OBJECT, security_info, None, None, dacl, None
        )
    except Exception as exc:  # noqa: BLE001 - Windows security APIs expose several error types
        raise OSError(f"无法设置仅当前用户可访问的 Windows DACL：{exc}") from exc


def file_permission_issue(path: Path) -> str | None:
    """Return a safe diagnostic when a config file is readable/writable too broadly."""
    try:
        if os.name != "nt":
            return "配置文件权限过宽，建议仅允许当前用户读取" if path.stat().st_mode & 0o077 else None
        import ntsecuritycon
        import win32api
        import win32security

        user_sid = win32security.LookupAccountName(None, win32api.GetUserName())[0]
        system_sid = win32security.CreateWellKnownSid(win32security.WinLocalSystemSid, None)
        admins_sid = win32security.CreateWellKnownSid(win32security.WinBuiltinAdministratorsSid, None)
        trusted_sids = {str(user_sid), str(system_sid), str(admins_sid)}
        descriptor = win32security.GetNamedSecurityInfo(
            str(path), win32security.SE_FILE_OBJECT, win32security.DACL_SECURITY_INFORMATION
        )
        dacl = descriptor.GetSecurityDescriptorDacl()
        if dacl is None:
            return "Windows DACL 缺失，无法保护配置文件"
        write_mask = (
            ntsecuritycon.FILE_GENERIC_WRITE
            | ntsecuritycon.FILE_WRITE_DATA
            | ntsecuritycon.FILE_APPEND_DATA
            | ntsecuritycon.WRITE_DAC
            | ntsecuritycon.DELETE
        )
        for index in range(dacl.GetAceCount()):
            ace = dacl.GetAce(index)
            if ace[0][0] == win32security.ACCESS_ALLOWED_ACE_TYPE and ace[1] & write_mask:
                if str(ace[2]) not in trusted_sids:
                    return "Windows DACL 向非受信主体授予了配置写权限"
    except Exception as exc:  # noqa: BLE001 - diagnostics must not expose ACL internals
        return f"无法验证 Windows DACL：{exc}"
    return None


def set_config_path(path: Path) -> None:
    """Set the current config path (used to derive data directory)."""
    global _current_config_path
    _current_config_path = path


def get_config_path() -> Path:
    """Get the configuration file path."""
    if _current_config_path:
        return _current_config_path
    return Path.home() / ".nanobot" / "config.json"


def load_config(config_path: Path | None = None, *, resolve_env: bool = False) -> Config:
    """
    Load configuration from file or create default.

    Args:
        config_path: Optional path to config file. Uses default if not provided.
        resolve_env: Resolve ``${NAME}`` references before Pydantic validation.
            Runtime entry points use this for numeric and boolean settings; editing
            surfaces keep the original placeholders by using the default.

    Returns:
        Loaded configuration object.
    """
    global _schema_refs_ready
    if not _schema_refs_ready:
        _resolve_tool_config_refs()
        _schema_refs_ready = True

    path = config_path or get_config_path()

    config = Config()
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            data = _migrate_config(data)
            if resolve_env:
                data = _resolve_env_vars(data)
            config = Config.model_validate(data)
        except (json.JSONDecodeError, ValueError, pydantic.ValidationError) as e:
            raise ValueError(f"Failed to load config from {path}: {e}") from e

    _apply_ssrf_whitelist(config)
    return config


def _apply_ssrf_whitelist(config: Config) -> None:
    """Apply SSRF whitelist from config to the network security module."""
    from nanobot.security.network import configure_ssrf_whitelist

    configure_ssrf_whitelist(config.tools.ssrf_whitelist)


def save_config(config: Config, config_path: Path | None = None) -> None:
    """
    Save configuration to file.

    Args:
        config: Configuration to save.
        config_path: Optional path to save to. Uses default if not provided.
    """
    path = config_path or get_config_path()
    data = config_to_dict(config)
    try:
        with FileLock(str(path) + ".lock", timeout=10):
            _write_json_atomic(path, data)
    except Timeout as exc:
        raise ValueError(f"Timed out waiting to write config {path}") from exc


def config_to_dict(config: Config) -> dict[str, Any]:
    """Serialize a config without resolving ``${ENV_VAR}`` placeholders."""
    data = config.model_dump(mode="json", by_alias=True)
    if config.providers.openai_codex.proxy is not None:
        data.setdefault("providers", {})["openaiCodex"] = {
            "proxy": config.providers.openai_codex.proxy,
        }
    return data


def _write_json_atomic(path: Path, data: Any) -> None:
    """Durably replace a JSON file without exposing a partial document."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        restrict_file_permissions(tmp_path)
        os.replace(tmp_path, path)
        if os.name != "nt":
            try:
                directory_fd = os.open(path.parent, os.O_RDONLY)
            except OSError:
                directory_fd = None
            if directory_fd is not None:
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def merge_missing_defaults(existing: Any, defaults: Any) -> Any:
    """Recursively add missing defaults without replacing configured values."""
    if not isinstance(existing, dict) or not isinstance(defaults, dict):
        return existing

    merged = dict(existing)
    for key, value in defaults.items():
        if key not in merged:
            merged[key] = value
        else:
            merged[key] = merge_missing_defaults(merged[key], value)
    return merged


_ENV_REF_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def resolve_config_env_vars(config: Config) -> Config:
    """Return *config* with ``${VAR}`` env-var references resolved.

    Walks in place so fields declared with ``exclude=True`` survive;
    returns the same instance when no references are present.
    Raises ``ValueError`` if a referenced variable is not set.
    """
    return _resolve_in_place(config)


def _resolve_in_place(obj: Any) -> Any:
    if isinstance(obj, str):
        new = _ENV_REF_PATTERN.sub(_env_replace, obj)
        return new if new != obj else obj
    if isinstance(obj, BaseModel):
        updates: dict[str, Any] = {}
        for name in type(obj).model_fields:
            old = getattr(obj, name)
            new = _resolve_in_place(old)
            if new is not old:
                updates[name] = new
        extras = obj.__pydantic_extra__
        new_extras: dict[str, Any] | None = None
        if extras:
            resolved = {k: _resolve_in_place(v) for k, v in extras.items()}
            if any(resolved[k] is not extras[k] for k in extras):
                new_extras = resolved
        if not updates and new_extras is None:
            return obj
        copy = obj.model_copy(update=updates) if updates else obj.model_copy()
        if new_extras is not None:
            copy.__pydantic_extra__ = new_extras
        return copy
    if isinstance(obj, dict):
        resolved = {k: _resolve_in_place(v) for k, v in obj.items()}
        return resolved if any(resolved[k] is not obj[k] for k in obj) else obj
    if isinstance(obj, list):
        resolved = [_resolve_in_place(v) for v in obj]
        return resolved if any(nv is not ov for nv, ov in zip(resolved, obj)) else obj
    return obj


def _resolve_env_vars(obj: object) -> object:
    """Recursively resolve ``${VAR}`` patterns in plain strings/dicts/lists."""
    if isinstance(obj, str):
        return _ENV_REF_PATTERN.sub(_env_replace, obj)
    if isinstance(obj, dict):
        return {k: _resolve_env_vars(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_env_vars(v) for v in obj]
    return obj


def _env_replace(match: re.Match[str]) -> str:
    name = match.group(1)
    value = os.environ.get(name)
    if value is None:
        raise ValueError(
            f"Environment variable '{name}' referenced in config is not set"
        )
    return value


def _migrate_config(data: dict) -> dict:
    """Migrate old config formats to current."""
    if not isinstance(data, dict):
        raise ValueError("Configuration root must be a JSON object")
    data = deepcopy(data)
    raw_version = data.get("schemaVersion", data.get("schema_version", 0))
    try:
        version = int(raw_version)
    except (TypeError, ValueError) as exc:
        raise ValueError("schemaVersion must be an integer") from exc
    if version < 0:
        raise ValueError("schemaVersion must not be negative")
    if version > CONFIG_SCHEMA_VERSION:
        raise ValueError(
            f"Configuration schemaVersion {version} is newer than this nanobot "
            f"release ({CONFIG_SCHEMA_VERSION})"
        )
    agents = data.get("agents", {})
    defaults = agents.get("defaults", {}) if isinstance(agents, dict) else {}
    if isinstance(defaults, dict):
        had_legacy_max_messages = (
            "maxMessages" in defaults or "max_messages" in defaults
        )
        defaults.pop("maxMessages", None)
        defaults.pop("max_messages", None)
        if had_legacy_max_messages:
            # TODO(next version): Remove this legacy cleanup branch; the schema
            # will silently ignore this field once the warning grace period ends.
            logger.warning(
                "agents.defaults.maxMessages/max_messages is legacy and ignored; "
                "replay max messages is now an internal safety cap. Remove it from "
                "config. This compatibility warning will be removed in the next version."
            )

    # Move tools.exec.restrictToWorkspace → tools.restrictToWorkspace
    tools = data.get("tools", {})
    exec_cfg = tools.get("exec", {})
    if "restrictToWorkspace" in exec_cfg and "restrictToWorkspace" not in tools:
        tools["restrictToWorkspace"] = exec_cfg.pop("restrictToWorkspace")

    # Move tools.myEnabled / tools.mySet → tools.my.{enable, allowSet}.
    # The old flat keys shipped in the initial MyTool landing; wrapping them in a
    # sub-config keeps `web` / `exec` / `my` symmetric and gives room to grow.
    if "myEnabled" in tools or "mySet" in tools:
        my_cfg = tools.setdefault("my", {})
        if "myEnabled" in tools and "enable" not in my_cfg:
            my_cfg["enable"] = tools.pop("myEnabled")
        else:
            tools.pop("myEnabled", None)
        if "mySet" in tools and "allowSet" not in my_cfg:
            my_cfg["allowSet"] = tools.pop("mySet")
        else:
            tools.pop("mySet", None)

    data.pop("schema_version", None)
    data["schemaVersion"] = CONFIG_SCHEMA_VERSION
    return data
