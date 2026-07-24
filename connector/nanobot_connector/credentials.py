"""On-device credential storage backed by the operating system keyring."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from pathlib import Path

from nanobot_connector.config import config_dir
from nanobot_connector.persistence import LocalStateError, locked_file, write_json_atomic


class CredentialStoreError(RuntimeError):
    """Raised when a secure credential backend is unavailable."""


@dataclass(frozen=True)
class CredentialMigrationResult:
    """Result of an explicit, local legacy-credential migration."""

    migrated_ids: tuple[str, ...]
    legacy_deleted: bool


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
        self._journal_path = config_dir() / "secrets.pending.json"
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
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise CredentialStoreError(f"本机凭据元数据文件无效：{path}") from exc
        if not isinstance(data, dict):
            raise CredentialStoreError(f"本机凭据元数据文件根节点必须是对象：{path}")
        return {str(k): str(v) for k, v in data.items()}

    @staticmethod
    def _write_mapping(path: Path, data: dict[str, str]) -> None:
        try:
            write_json_atomic(path, data)
        except LocalStateError as exc:
            raise CredentialStoreError(str(exc)) from exc

    @staticmethod
    def _validate_secret_id(secret_id: str) -> str:
        if not isinstance(secret_id, str) or not (normalized := secret_id.strip()):
            raise CredentialStoreError("凭据标识不能为空")
        if any(ord(char) < 32 for char in normalized):
            raise CredentialStoreError("凭据标识不能包含控制字符")
        return normalized

    def _index(self) -> set[str]:
        return set(self._read_mapping(self._index_path))

    def _save_index(self, ids: set[str]) -> None:
        self._write_mapping(self._index_path, {secret_id: "" for secret_id in sorted(ids)})

    def _read_journal(self) -> tuple[str, str] | None:
        try:
            data = json.loads(self._journal_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise CredentialStoreError("凭据操作恢复日志损坏，拒绝继续修改凭据") from exc
        if not isinstance(data, dict):
            raise CredentialStoreError("凭据操作恢复日志格式无效，拒绝继续修改凭据")
        operation = data.get("operation")
        secret_id = data.get("secretId")
        if operation not in {"set", "delete"} or not isinstance(secret_id, str):
            raise CredentialStoreError("凭据操作恢复日志格式无效，拒绝继续修改凭据")
        return operation, self._validate_secret_id(secret_id)

    def _write_journal(self, operation: str, secret_id: str) -> None:
        self._write_mapping(self._journal_path, {"operation": operation, "secretId": secret_id})

    def _clear_journal(self) -> None:
        try:
            self._journal_path.unlink(missing_ok=True)
        except OSError as exc:
            raise CredentialStoreError("无法清理凭据操作恢复日志") from exc

    def _get_secure(self, secret_id: str) -> str | None:
        try:
            return self._keyring().get_password(self._service, secret_id)
        except Exception as exc:  # keyring backend exceptions are platform-specific
            raise CredentialStoreError("无法从操作系统凭据库读取凭据") from exc

    def _recover_pending_unlocked(self) -> None:
        """Complete the last interrupted index/keyring update under the file lock."""
        pending = self._read_journal()
        if pending is None:
            return
        operation, secret_id = pending
        if operation == "set":
            # The secret may have been written just before a process crash. If it
            # was not, dropping the prepared journal is safe and avoids indexing a
            # value that does not exist.
            if self._get_secure(secret_id) is not None:
                ids = self._index()
                ids.add(secret_id)
                self._save_index(ids)
        else:
            # A delete is intentionally completed on recovery: the journal is an
            # explicit locally requested deletion, never an untrusted remote input.
            if self._get_secure(secret_id) is not None:
                try:
                    self._keyring().delete_password(self._service, secret_id)
                except Exception as exc:  # keyring errors differ by platform
                    raise CredentialStoreError("无法恢复未完成的凭据删除") from exc
            ids = self._index()
            ids.discard(secret_id)
            self._save_index(ids)
        self._clear_journal()

    def get(self, secret_id: str) -> str | None:
        secret_id = self._validate_secret_id(secret_id)
        if self._file_backend:
            return self._read_mapping(self._legacy_path).get(secret_id)
        return self._get_secure(secret_id)

    def set(self, secret_id: str, value: str) -> None:
        secret_id = self._validate_secret_id(secret_id)
        if self._file_backend:
            data = self._read_mapping(self._legacy_path)
            data[secret_id] = value
            self._write_mapping(self._legacy_path, data)
            return
        try:
            with locked_file(self._index_path):
                self._recover_pending_unlocked()
                self._write_journal("set", secret_id)
                try:
                    self._keyring().set_password(self._service, secret_id, value)
                    ids = self._index()
                    ids.add(secret_id)
                    self._save_index(ids)
                except Exception as exc:  # keyring backend exceptions are platform-specific
                    raise CredentialStoreError(
                        "写入凭据或索引失败；下次操作会自动恢复未完成的本机事务"
                    ) from exc
                self._clear_journal()
        except LocalStateError as exc:
            raise CredentialStoreError(str(exc)) from exc

    def delete(self, secret_id: str) -> bool:
        secret_id = self._validate_secret_id(secret_id)
        if self._file_backend:
            data = self._read_mapping(self._legacy_path)
            if secret_id not in data:
                return False
            del data[secret_id]
            self._write_mapping(self._legacy_path, data)
            return True
        try:
            with locked_file(self._index_path):
                self._recover_pending_unlocked()
                if self._get_secure(secret_id) is None:
                    # Repair a stale index entry while retaining the caller's
                    # original "not found" result.
                    ids = self._index()
                    if secret_id in ids:
                        ids.discard(secret_id)
                        self._save_index(ids)
                    return False
                self._write_journal("delete", secret_id)
                try:
                    self._keyring().delete_password(self._service, secret_id)
                    ids = self._index()
                    ids.discard(secret_id)
                    self._save_index(ids)
                except Exception as exc:  # keyring backend exceptions are platform-specific
                    raise CredentialStoreError(
                        "删除凭据或索引失败；下次操作会自动恢复未完成的本机事务"
                    ) from exc
                self._clear_journal()
                return True
        except LocalStateError as exc:
            raise CredentialStoreError(str(exc)) from exc

    def ids(self) -> list[str]:
        if self._file_backend:
            return sorted(self._read_mapping(self._legacy_path))
        try:
            with locked_file(self._index_path):
                self._recover_pending_unlocked()
                return sorted(self._index())
        except LocalStateError as exc:
            raise CredentialStoreError(str(exc)) from exc

    def integrity_issues(self) -> list[str]:
        """Report index references whose values no longer exist in the keyring."""
        if self._file_backend:
            return []
        try:
            with locked_file(self._index_path):
                self._recover_pending_unlocked()
                return [
                    f"凭据索引引用了不存在的凭据：{secret_id}"
                    for secret_id in sorted(self._index())
                    if self._get_secure(secret_id) is None
                ]
        except LocalStateError as exc:
            raise CredentialStoreError(str(exc)) from exc

    def secure_backend_available(self) -> bool:
        if self._file_backend:
            return False
        try:
            self._keyring()
        except CredentialStoreError:
            return False
        return True

    def migrate_legacy(self, *, delete_after_success: bool = False) -> CredentialMigrationResult:
        """Move a legacy plaintext file into the OS keyring on this device only."""
        if self._file_backend:
            raise CredentialStoreError("迁移必须使用默认的操作系统凭据库")
        legacy = self._read_mapping(self._legacy_path)
        for secret_id, value in legacy.items():
            self.set(secret_id, value)
            stored = self.get(secret_id)
            if stored is None or not hmac.compare_digest(stored, value):
                raise CredentialStoreError("历史凭据写入验证失败；明文源文件未删除")
        if legacy and delete_after_success:
            try:
                self._legacy_path.unlink()
            except OSError as exc:
                raise CredentialStoreError("历史凭据已迁移，但无法删除明文源文件") from exc
        return CredentialMigrationResult(tuple(sorted(legacy)), bool(legacy and delete_after_success))
