"""On-device credential storage backed by the operating system keyring."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from nanobot_connector.config import config_dir


class CredentialStoreError(RuntimeError):
    """Raised when a secure credential backend is unavailable."""


class SecretStore:
    """Store credential values locally without serializing them to JSON.

    Passing *path* selects the legacy file backend solely for tests and explicit
    local migration tooling. Normal connector operation uses ``keyring`` and
    keeps only credential identifiers in a metadata file.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._legacy_path = path or (config_dir() / "secrets.json")
        self._file_backend = path is not None
        self._index_path = config_dir() / "secrets.index.json"
        identity = str(config_dir().resolve()).encode("utf-8")
        self._service = f"nanobot-connector:{hashlib.sha256(identity).hexdigest()[:16]}"

    def _keyring(self):
        try:
            import keyring
            from keyring.errors import KeyringError
        except ModuleNotFoundError as exc:
            raise CredentialStoreError("未安装操作系统凭据库依赖 keyring") from exc
        try:
            backend = keyring.get_keyring()
            if getattr(backend, "priority", 0) <= 0:
                raise CredentialStoreError("当前系统没有可用的安全凭据库")
        except KeyringError as exc:
            raise CredentialStoreError("无法访问操作系统凭据库") from exc
        return keyring

    @staticmethod
    def _read_mapping(path: Path) -> dict[str, str]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {}
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}

    @staticmethod
    def _write_mapping(path: Path, data: dict[str, str]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
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

    def _index(self) -> set[str]:
        return set(self._read_mapping(self._index_path))

    def _save_index(self, ids: set[str]) -> None:
        self._write_mapping(self._index_path, {secret_id: "" for secret_id in sorted(ids)})

    def get(self, secret_id: str) -> str | None:
        if self._file_backend:
            return self._read_mapping(self._legacy_path).get(secret_id)
        try:
            return self._keyring().get_password(self._service, secret_id)
        except Exception as exc:  # keyring backend exceptions are platform-specific
            raise CredentialStoreError("无法从操作系统凭据库读取凭据") from exc

    def set(self, secret_id: str, value: str) -> None:
        if self._file_backend:
            data = self._read_mapping(self._legacy_path)
            data[secret_id] = value
            self._write_mapping(self._legacy_path, data)
            return
        try:
            self._keyring().set_password(self._service, secret_id, value)
        except Exception as exc:  # keyring backend exceptions are platform-specific
            raise CredentialStoreError("无法写入操作系统凭据库") from exc
        ids = self._index()
        ids.add(secret_id)
        self._save_index(ids)

    def delete(self, secret_id: str) -> bool:
        if self._file_backend:
            data = self._read_mapping(self._legacy_path)
            if secret_id not in data:
                return False
            del data[secret_id]
            self._write_mapping(self._legacy_path, data)
            return True
        if self.get(secret_id) is None:
            return False
        try:
            self._keyring().delete_password(self._service, secret_id)
        except Exception as exc:  # keyring backend exceptions are platform-specific
            raise CredentialStoreError("无法从操作系统凭据库删除凭据") from exc
        ids = self._index()
        ids.discard(secret_id)
        self._save_index(ids)
        return True

    def ids(self) -> list[str]:
        if self._file_backend:
            return sorted(self._read_mapping(self._legacy_path))
        return sorted(self._index())

    def secure_backend_available(self) -> bool:
        if self._file_backend:
            return False
        try:
            self._keyring()
        except CredentialStoreError:
            return False
        return True

    def migrate_legacy(self, *, delete_after_success: bool = False) -> list[str]:
        """Move a legacy plaintext file into the OS keyring on this device only."""
        if self._file_backend:
            raise CredentialStoreError("迁移必须使用默认的操作系统凭据库")
        legacy = self._read_mapping(self._legacy_path)
        for secret_id, value in legacy.items():
            self.set(secret_id, value)
        if legacy and delete_after_success:
            self._legacy_path.unlink(missing_ok=True)
        return sorted(legacy)
