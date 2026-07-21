"""Connector client download manifest for the WebUI devices wizard."""

from __future__ import annotations

from typing import Any

_DEFAULT_VERSION = "0.1.0"
_GITHUB_REPO = "HKUDS/nanobot"
_SOURCE_INSTALL = (
    'pip install "nanobot-connector @ git+https://github.com/HKUDS/nanobot.git#subdirectory=connector"'
)

_PLATFORM_SPECS: tuple[tuple[str, str, str], ...] = (
    ("windows", "Windows", "nanobot-connector.exe"),
    ("macos", "macOS", "nanobot-connector"),
    ("linux", "Linux", "nanobot-connector"),
)


def connector_downloads_payload(config: Any) -> dict[str, Any]:
    """Build download links for the connector client installer.

    When ``download_base_url`` is set (admin mirror), per-platform URLs point
    there. Otherwise they target GitHub Release assets under ``connector-v*``
    tags. The releases page link is always included as a fallback when assets
    are not yet published.
    """
    version = (getattr(config, "download_version", None) or _DEFAULT_VERSION).strip()
    tag = f"connector-v{version}"
    releases_url = (
        getattr(config, "releases_url", None)
        or f"https://github.com/{_GITHUB_REPO}/releases?q=connector"
    ).strip()
    mirror_base = (getattr(config, "download_base_url", None) or "").strip().rstrip("/")

    platforms: list[dict[str, str]] = []
    for platform_id, label, filename in _PLATFORM_SPECS:
        if mirror_base:
            url = f"{mirror_base}/{filename}"
        else:
            url = f"https://github.com/{_GITHUB_REPO}/releases/download/{tag}/{filename}"
        platforms.append(
            {
                "id": platform_id,
                "label": label,
                "filename": filename,
                "url": url,
            }
        )

    return {
        "version": version,
        "tag": tag,
        "releasesUrl": releases_url,
        "sourceInstall": _SOURCE_INSTALL,
        "platforms": platforms,
    }
