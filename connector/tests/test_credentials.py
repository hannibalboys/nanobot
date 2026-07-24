from __future__ import annotations

import json

import pytest

from nanobot_connector.config import config_dir
from nanobot_connector.credentials import CredentialStoreError, SecretStore


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("NANOBOT_CONNECTOR_HOME", str(tmp_path / "home"))


class _FakeKeyring:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, secret_id: str) -> str | None:
        return self.values.get((service, secret_id))

    def set_password(self, service: str, secret_id: str, value: str) -> None:
        self.values[(service, secret_id)] = value

    def delete_password(self, service: str, secret_id: str) -> None:
        del self.values[(service, secret_id)]


def test_secure_store_keeps_values_out_of_index(monkeypatch) -> None:
    store = SecretStore()
    backend = _FakeKeyring()
    monkeypatch.setattr(store, "_keyring", lambda: backend)

    store.set("api", "private-value")

    assert store.get("api") == "private-value"
    assert store.ids() == ["api"]
    index = (config_dir() / "secrets.index.json").read_text(encoding="utf-8")
    assert "private-value" not in index


def test_explicit_legacy_migration_is_local_and_opt_in(monkeypatch) -> None:
    legacy_path = config_dir() / "secrets.json"
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_text(json.dumps({"legacy": "value"}), encoding="utf-8")
    store = SecretStore()
    backend = _FakeKeyring()
    monkeypatch.setattr(store, "_keyring", lambda: backend)

    result = store.migrate_legacy(delete_after_success=True)

    assert result.migrated_ids == ("legacy",)
    assert result.legacy_deleted is True
    assert store.get("legacy") == "value"
    assert not legacy_path.exists()


def test_file_backend_is_explicit_legacy_only(tmp_path) -> None:
    store = SecretStore(tmp_path / "secrets.json")
    store.set("legacy", "value")

    assert store.get("legacy") == "value"
    assert store.secure_backend_available() is False
    with pytest.raises(CredentialStoreError):
        store.migrate_legacy()


def test_failed_index_write_is_recovered_before_the_next_operation(monkeypatch) -> None:
    store = SecretStore()
    backend = _FakeKeyring()
    monkeypatch.setattr(store, "_keyring", lambda: backend)
    original_save = store._save_index

    def fail_once(_ids: set[str]) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(store, "_save_index", fail_once)
    with pytest.raises(CredentialStoreError, match="自动恢复"):
        store.set("api", "private-value")

    assert store.get("api") == "private-value"
    assert (config_dir() / "secrets.pending.json").exists()

    monkeypatch.setattr(store, "_save_index", original_save)
    assert store.ids() == ["api"]
    assert not (config_dir() / "secrets.pending.json").exists()


def test_integrity_check_reports_stale_index(monkeypatch) -> None:
    store = SecretStore()
    backend = _FakeKeyring()
    monkeypatch.setattr(store, "_keyring", lambda: backend)
    store.set("api", "private-value")
    backend.values.clear()

    assert store.integrity_issues() == ["凭据索引引用了不存在的凭据：api"]
