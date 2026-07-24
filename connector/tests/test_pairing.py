"""Tests for pairing helpers."""

from __future__ import annotations

import hashlib
from unittest.mock import patch

import pytest

from nanobot_connector.config import ConnectorClientConfig
from nanobot_connector.gui import _parse_paste
from nanobot_connector.pairing import (
    PairingError,
    _verify_certificate_fingerprint,
    normalize_pairing_code,
    pair_device,
)


def test_normalize_pairing_code_strips_spaces() -> None:
    assert normalize_pairing_code("7 1 K 9 Q T E D") == "71K9QTED"


def test_parse_paste_command() -> None:
    parsed = _parse_paste(
        "nanobot-connector pair --server ws://192.168.1.10:18790 --code 71K9QTED"
    )
    assert parsed == ("ws://192.168.1.10:18790", "71K9QTED")


def test_normalize_server_url_fixes_legacy_port() -> None:
    from nanobot_connector.gui import _normalize_server_url

    assert _normalize_server_url("ws://127.0.0.1:18790") == "ws://127.0.0.1:8765"


def test_repair_to_new_server_requires_explicit_confirmation() -> None:
    cfg = ConnectorClientConfig(server="wss://old.example", device_token="old", node_id="old-node")

    with pytest.raises(PairingError, match="--replace-server"):
        pair_device(cfg, server="wss://new.example", code="ABCD")

    assert cfg.server == "wss://old.example"
    assert cfg.device_token == "old"


def test_invalid_pairing_port_is_a_user_facing_error() -> None:
    cfg = ConnectorClientConfig()

    with pytest.raises(PairingError, match="端口无效"):
        pair_device(cfg, server="wss://example.test:not-a-port", code="ABCD")


def test_repairing_same_server_retains_existing_certificate_pin() -> None:
    pin = "ab" * 32
    cfg = ConnectorClientConfig(server="wss://example.test", cert_fingerprint=pin)
    payload = {"nodeId": "node", "token": "token"}

    with patch("nanobot_connector.pairing._http_get_json", return_value=payload), patch.object(
        ConnectorClientConfig, "save"
    ):
        pair_device(cfg, server="wss://example.test", code="ABCD")

    assert cfg.cert_fingerprint == pin


def test_initial_pair_saves_an_untracked_config(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NANOBOT_CONNECTOR_HOME", str(tmp_path / "home"))
    cfg = ConnectorClientConfig()

    with patch(
        "nanobot_connector.pairing._http_get_json", return_value={"nodeId": "node", "token": "token"}
    ):
        pair_device(cfg, server="wss://example.test", code="ABCD")

    assert cfg.node_id == "node"
    assert ConnectorClientConfig.load().device_token == "token"


def test_failed_repair_preserves_existing_identity() -> None:
    cfg = ConnectorClientConfig(server="wss://old.example", device_token="old", node_id="old-node")

    with patch("nanobot_connector.pairing._http_get_json", side_effect=OSError("offline")):
        with pytest.raises(PairingError, match="配对失败"):
            pair_device(
                cfg,
                server="wss://new.example",
                code="ABCD",
                replace_server=True,
            )

    assert cfg.server == "wss://old.example"
    assert cfg.device_token == "old"
    assert cfg.node_id == "old-node"


def test_certificate_fingerprint_is_compared() -> None:
    certificate = b"test-certificate"
    expected = hashlib.sha256(certificate).hexdigest()

    class Socket:
        def getpeercert(self, *, binary_form: bool):
            assert binary_form is True
            return certificate

    class Response:
        class fp:  # noqa: N801 - mirrors urllib response shape
            class raw:  # noqa: N801 - mirrors urllib response shape
                _sock = Socket()

    _verify_certificate_fingerprint(Response(), expected)
    with pytest.raises(PairingError, match="不匹配"):
        _verify_certificate_fingerprint(Response(), "00" * 32)
