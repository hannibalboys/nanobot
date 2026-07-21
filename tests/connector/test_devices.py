"""Tests for DeviceStore and PairRateLimiter (tasks 2.1, 2.3, 2.4)."""

import time

import pytest

from nanobot.connector.devices import DeviceStore, PairRateLimiter


@pytest.fixture
def store(tmp_path):
    return DeviceStore(tmp_path / "connector" / "devices.json", pairing_ttl_s=600)


def test_pairing_code_redeem_issues_token(store):
    code, _ = store.generate_pairing_code("webui:xu")
    result = store.redeem_pairing_code(
        code, name="Xu PC", platform="windows", machine_fingerprint="fp1"
    )
    assert result is not None
    node_id, token = result
    assert node_id.startswith("dev-")
    assert token
    device = store.verify_token(token)
    assert device is not None
    assert device.node_id == node_id
    assert device.owner_id == "webui:xu"


def test_pairing_code_single_use(store):
    code, _ = store.generate_pairing_code("webui:xu")
    assert store.redeem_pairing_code(
        code, name="a", platform="windows", machine_fingerprint="fp1"
    )
    # second redemption rejected
    assert (
        store.redeem_pairing_code(
            code, name="a", platform="windows", machine_fingerprint="fp2"
        )
        is None
    )


def test_pairing_code_expiry(tmp_path):
    store = DeviceStore(tmp_path / "devices.json", pairing_ttl_s=0)
    code, _ = store.generate_pairing_code("webui:xu")
    time.sleep(0.01)
    assert (
        store.redeem_pairing_code(
            code, name="a", platform="windows", machine_fingerprint="fp1"
        )
        is None
    )


def test_only_token_hash_persisted(store):
    code, _ = store.generate_pairing_code("webui:xu")
    _, token = store.redeem_pairing_code(
        code, name="a", platform="windows", machine_fingerprint="fp1"
    )
    raw = store._path.read_text(encoding="utf-8")
    assert token not in raw
    assert "tokenSha256" in raw


def test_persistence_survives_reload(store):
    code, _ = store.generate_pairing_code("webui:xu")
    node_id, token = store.redeem_pairing_code(
        code, name="a", platform="windows", machine_fingerprint="fp1"
    )
    reloaded = DeviceStore(store._path)
    device = reloaded.verify_token(token)
    assert device is not None
    assert device.node_id == node_id


def test_revoke_blocks_token(store):
    code, _ = store.generate_pairing_code("webui:xu")
    node_id, token = store.redeem_pairing_code(
        code, name="a", platform="windows", machine_fingerprint="fp1"
    )
    assert store.revoke(node_id) is True
    assert store.verify_token(token) is None
    # revoked device excluded from listing
    assert all(d.node_id != node_id for d in store.list_devices())


def test_revoke_enforces_ownership(store):
    code, _ = store.generate_pairing_code("webui:a")
    node_id, _ = store.redeem_pairing_code(
        code, name="a", platform="windows", machine_fingerprint="fp1"
    )
    assert store.revoke(node_id, owner_id="webui:b") is False
    assert store.revoke(node_id, owner_id="webui:a") is True


def test_fingerprint_dedup_replaces_and_revokes_old(store):
    code1, _ = store.generate_pairing_code("webui:xu")
    node1, token1 = store.redeem_pairing_code(
        code1, name="a", platform="windows", machine_fingerprint="fp-same"
    )
    code2, _ = store.generate_pairing_code("webui:xu")
    node2, token2 = store.redeem_pairing_code(
        code2, name="a2", platform="windows", machine_fingerprint="fp-same"
    )
    assert node1 == node2  # same record reused
    assert store.verify_token(token1) is None  # old token dead
    assert store.verify_token(token2) is not None
    assert len(store.list_devices()) == 1


def test_owner_filter_in_listing(store):
    for owner in ("webui:a", "webui:b"):
        code, _ = store.generate_pairing_code(owner)
        store.redeem_pairing_code(
            code, name=owner, platform="windows", machine_fingerprint=owner
        )
    assert len(store.list_devices(owner_id="webui:a")) == 1
    assert len(store.list_devices()) == 2


# -- rate limiter -----------------------------------------------------------


def test_ip_lockout_after_threshold():
    rl = PairRateLimiter(max_failures=3, lockout_s=900)
    assert not rl.is_locked("1.2.3.4")
    for _ in range(3):
        rl.record_failure("1.2.3.4")
    assert rl.is_locked("1.2.3.4")


def test_success_resets_failures():
    rl = PairRateLimiter(max_failures=3)
    rl.record_failure("1.2.3.4")
    rl.record_failure("1.2.3.4")
    rl.record_success("1.2.3.4")
    rl.record_failure("1.2.3.4")
    assert not rl.is_locked("1.2.3.4")


def test_global_breaker_trips():
    rl = PairRateLimiter(max_failures=1000, global_threshold=5, global_window_s=300)
    tripped = False
    for i in range(5):
        tripped = rl.record_failure(f"10.0.0.{i}")
    assert tripped is True
