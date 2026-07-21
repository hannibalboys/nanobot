"""Tests for pairing helpers."""

from __future__ import annotations

from nanobot_connector.gui import _parse_paste
from nanobot_connector.pairing import normalize_pairing_code


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
