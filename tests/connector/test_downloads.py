"""Tests for connector client download manifest."""

from nanobot.config.schema import ConnectorConfig
from nanobot.connector.downloads import connector_downloads_payload


def test_downloads_default_github_urls() -> None:
    payload = connector_downloads_payload(ConnectorConfig(enabled=True))
    assert payload["version"] == "0.1.0"
    assert payload["tag"] == "connector-v0.1.0"
    assert "releases?q=connector" in payload["releasesUrl"]
    assert payload["platforms"][0]["id"] == "windows"
    assert payload["platforms"][0]["url"].endswith("/connector-v0.1.0/nanobot-connector.exe")
    assert "git+https://github.com/HKUDS/nanobot.git#subdirectory=connector" in payload["sourceInstall"]


def test_downloads_mirror_base_url() -> None:
    config = ConnectorConfig(
        enabled=True,
        download_base_url="https://files.example.com/connector",
    )
    payload = connector_downloads_payload(config)
    assert payload["platforms"][1]["url"] == "https://files.example.com/connector/nanobot-connector"
