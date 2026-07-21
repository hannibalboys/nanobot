"""Connector client configuration persisted at ``~/.nanobot-connector/config.json``."""

from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


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
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self.model_dump(by_alias=True), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(path)

    @classmethod
    def load(cls) -> "ConnectorClientConfig":
        try:
            data = json.loads(config_path().read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return cls()
        return cls.model_validate(data)
