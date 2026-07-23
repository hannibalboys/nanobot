"""Portable, explicit bootstrap helpers for a connector device."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from nanobot_connector.config import ConnectorClientConfig, config_dir, config_path
from nanobot_connector.credentials import SecretStore
from nanobot_connector.tools import ToolDef, ToolRegistry

ConflictPolicy = Literal["fail", "skip", "replace"]


class ConnectorBootstrapError(ValueError):
    """A safe, user-facing connector bootstrap error."""


@dataclass(frozen=True)
class ConnectorDoctorResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    details: dict[str, object] = field(default_factory=dict)


def templates_dir() -> Path:
    return Path(__file__).with_name("templates")


def list_templates() -> list[str]:
    return sorted(path.stem for path in templates_dir().glob("*.json") if path.is_file())


def _template_path(name: str) -> Path:
    if not name or Path(name).name != name or name.endswith(".json"):
        raise ConnectorBootstrapError("工具档案名称无效")
    path = templates_dir() / f"{name}.json"
    if not path.is_file():
        raise ConnectorBootstrapError(f"未找到工具档案：{name}")
    return path


def _load_template(name: str) -> list[ToolDef]:
    path = _template_path(name)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConnectorBootstrapError(f"工具档案无效：{exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("tools"), list):
        raise ConnectorBootstrapError("工具档案必须包含 tools 数组")
    try:
        tools = [ToolDef.model_validate(item) for item in data["tools"]]
    except ValueError as exc:
        raise ConnectorBootstrapError(f"工具档案定义无效：{exc}") from exc
    for tool in tools:
        if tool.env:
            raise ConnectorBootstrapError(f"工具档案不得包含内联环境变量：{tool.name}")
    return tools


def _executable_available(executable: str) -> bool:
    path = Path(executable).expanduser()
    return path.is_file() if path.is_absolute() else shutil.which(executable) is not None


def initialize_connector(*, home: Path | None = None) -> ConnectorClientConfig:
    """Create only safe local state; never pair or share directories."""
    if home is not None:
        os.environ["NANOBOT_CONNECTOR_HOME"] = str(home.expanduser().resolve())
    cfg = ConnectorClientConfig.load()
    if not config_path().exists():
        cfg.save()
    tools_path = config_dir() / "tools.json"
    if not tools_path.exists():
        ToolRegistry().save()
    return cfg


def import_template(name: str, *, on_conflict: ConflictPolicy = "fail") -> list[str]:
    """Preflight then atomically add a bundled tool archive to ``tools.json``."""
    if on_conflict not in {"fail", "skip", "replace"}:
        raise ConnectorBootstrapError("冲突策略必须是 fail、skip 或 replace")
    tools = _load_template(name)
    missing = [tool.exec for tool in tools if not _executable_available(tool.exec)]
    if missing:
        raise ConnectorBootstrapError(f"当前设备缺少可执行文件：{', '.join(missing)}")
    registry = ToolRegistry.load()
    existing = {tool.name for tool in registry.list()}
    conflicts = [tool.name for tool in tools if tool.name in existing]
    if conflicts and on_conflict == "fail":
        raise ConnectorBootstrapError(f"已存在同名工具：{', '.join(conflicts)}")
    imported: list[str] = []
    for tool in tools:
        if tool.name in existing and on_conflict == "skip":
            continue
        registry.add(tool)
        imported.append(tool.name)
    if imported:
        registry.save()
    return imported


def export_tool_template(name: str, output: Path) -> None:
    """Export one reviewable tool definition without inline environment values."""
    tool = ToolRegistry.load().get(name)
    if Path(tool.exec).is_absolute():
        raise ConnectorBootstrapError("不能导出机器专属绝对可执行文件路径，请手动创建可移植档案")
    payload = tool.model_dump(by_alias=True)
    payload["env"] = {}
    target = output.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps({"tools": [payload]}, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, target)


def doctor_connector(*, strict: bool = False) -> ConnectorDoctorResult:
    """Check a device without changing pairing, tools, or credentials."""
    errors: list[str] = []
    warnings: list[str] = []
    try:
        cfg = ConnectorClientConfig.load()
    except ValueError as exc:
        return ConnectorDoctorResult(False, errors=[str(exc)])
    if not cfg.server or not cfg.device_token:
        warnings.append("设备尚未配对；请在 WebUI 生成配对码后执行 pair")
    missing_roots = [root for root in cfg.roots if not Path(root).is_dir()]
    if missing_roots:
        warnings.append(f"共享目录不存在：{', '.join(missing_roots)}")
    try:
        tools = ToolRegistry.load().list()
    except Exception as exc:  # noqa: BLE001 - diagnostics must stay readable
        errors.append(f"无法读取本机工具注册表：{exc}")
        tools = []
    missing_tools = [tool.name for tool in tools if not _executable_available(tool.exec)]
    if missing_tools:
        message = f"工具可执行文件缺失：{', '.join(missing_tools)}"
        (errors if strict else warnings).append(message)
    if cfg.insecure:
        message = "当前连接器启用了 --insecure，生产环境不得使用"
        (errors if strict else warnings).append(message)
    if not SecretStore().secure_backend_available():
        message = "当前系统没有可用的操作系统凭据库，不能安全保存工具凭据"
        (errors if strict else warnings).append(message)
    return ConnectorDoctorResult(
        ok=not errors,
        errors=errors,
        warnings=warnings,
        details={
            "home": str(config_dir()),
            "paired": bool(cfg.server and cfg.device_token),
            "tools": [tool.name for tool in tools],
            "desktopEnabled": cfg.desktop_enabled,
        },
    )
