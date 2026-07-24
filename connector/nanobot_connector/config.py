"""Connector client configuration persisted at ``~/.nanobot-connector/config.json``."""

from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr
from pydantic.alias_generators import to_camel

from nanobot_connector.persistence import (
    LocalStateConflictError,
    file_fingerprint,
    locked_file,
    write_json_atomic,
)

CONNECTOR_CONFIG_VERSION = 1


class ConnectorConfigConflictError(LocalStateConflictError):
    """Raised when another local process changed connector config first."""


def config_dir() -> Path:
    override = os.environ.get("NANOBOT_CONNECTOR_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".nanobot-connector"


def config_path() -> Path:
    return config_dir() / "config.json"


class ConnectorClientConfig(BaseModel):
    """Local connector configuration (camelCase on disk)."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    config_version: int = CONNECTOR_CONFIG_VERSION
    server: str = ""  # wss://host:port
    device_token: str = ""
    node_id: str = ""
    name: str = ""
    fingerprint: str = ""
    roots: list[str] = Field(default_factory=list)
    cert_fingerprint: str = ""  # pinned server cert sha256 (self-signed)
    insecure: bool = False  # skip TLS verification (explicit, discouraged)
    chunk_bytes: int = 262_144
    max_file_bytes: int = 209_715_200
    # Controlled execution self-limits (defense in depth; server limits also apply).
    exec_timeout_s: int = 300  # fallback when a tool declares no timeout
    max_exec_output_bytes: int = 1_048_576  # total stdout+stderr cap per call
    # Desktop control opt-in (add-connector-desktop-control). Off by default; the
    # owner enables it locally, and each session still needs on-device approval.
    desktop_enabled: bool = False
    desktop_max_fps: int = 2
    desktop_max_dimension: int = 1280
    _snapshot: str | None = PrivateAttr(default=None)
    _loaded_from_disk: bool = PrivateAttr(default=False)

    def save(self) -> None:
        path = config_path()
        with locked_file(path):
            current = file_fingerprint(path)
            if self._loaded_from_disk and current != self._snapshot:
                raise ConnectorConfigConflictError(
                    "连接器配置已被另一个本机进程修改；请重新加载后再提交修改"
                )
            write_json_atomic(path, self.model_dump(by_alias=True))
            self._snapshot = file_fingerprint(path)
            self._loaded_from_disk = True

    @classmethod
    def load(cls) -> "ConnectorClientConfig":
        try:
            data = json.loads(config_path().read_text(encoding="utf-8"))
        except FileNotFoundError:
            config = cls()
            config._snapshot = file_fingerprint(config_path())
            config._loaded_from_disk = True
            return config
        except (OSError, ValueError) as exc:
            raise ValueError("connector 配置文件不是有效 JSON") from exc
        if not isinstance(data, dict):
            raise ValueError("connector config root must be an object")
        version = data.get("configVersion", data.get("config_version", 0))
        try:
            version = int(version)
        except (TypeError, ValueError) as exc:
            raise ValueError("connector configVersion must be an integer") from exc
        if version > CONNECTOR_CONFIG_VERSION:
            raise ValueError(
                f"connector configVersion {version} is newer than this client "
                f"({CONNECTOR_CONFIG_VERSION})"
            )
        data.pop("config_version", None)
        data["configVersion"] = CONNECTOR_CONFIG_VERSION
        config = cls.model_validate(data)
        config._snapshot = file_fingerprint(config_path())
        config._loaded_from_disk = True
        return config
