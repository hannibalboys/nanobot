"""Connector client configuration persisted at ``~/.nanobot-connector/config.json``."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

CONNECTOR_CONFIG_VERSION = 1


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

    def save(self) -> None:
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self.model_dump(by_alias=True), handle, indent=2, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    @classmethod
    def load(cls) -> "ConnectorClientConfig":
        try:
            data = json.loads(config_path().read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return cls()
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
        return cls.model_validate(data)
