"""HTTP pairing helpers shared by the CLI and GUI."""

from __future__ import annotations

import json
import platform
import ssl
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from nanobot_connector.client import machine_fingerprint
from nanobot_connector.config import ConnectorClientConfig


class PairingError(Exception):
    """Raised when the server rejects or cannot complete pairing."""


def normalize_pairing_code(code: str) -> str:
    return code.strip().replace(" ", "").upper()


def pair_device(
    cfg: ConnectorClientConfig,
    *,
    server: str,
    code: str,
    name: str = "",
    fingerprint: str = "",
    insecure: bool = False,
) -> ConnectorClientConfig:
    """Redeem a pairing code and persist the device token."""
    server = server.strip()
    code = normalize_pairing_code(code)
    if not server:
        raise PairingError("请填写网关地址")
    if not code:
        raise PairingError("请填写配对码")

    cfg.server = server
    cfg.name = name.strip()
    cfg.cert_fingerprint = fingerprint.replace(":", "")
    cfg.insecure = insecure
    fp = machine_fingerprint()
    cfg.fingerprint = fp

    parts = urlsplit(server)
    scheme = "https" if parts.scheme in ("wss", "https") else "http"
    query = urlencode({
        "code": code,
        "name": cfg.name or "",
        "platform": platform.system().lower(),
        "fingerprint": fp,
    })
    url = f"{scheme}://{parts.netloc}/connector/pair?{query}"
    try:
        payload = _http_get_json(url, cfg)
    except Exception as exc:  # noqa: BLE001 - user-facing message
        hint = ""
        if scheme == "https" and not cfg.cert_fingerprint and not cfg.insecure:
            hint = " 若服务器使用自签证书，请联系管理员获取证书指纹。"
        raise PairingError(f"配对失败：{exc}{hint}") from exc

    cfg.node_id = payload["nodeId"]
    cfg.device_token = payload["token"]
    cfg.save()
    return cfg


def _http_get_json(url: str, cfg: ConnectorClientConfig) -> dict:
    context = None
    if url.startswith("https"):
        context = ssl.create_default_context()
        if cfg.insecure or cfg.cert_fingerprint:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
    req = Request(url, method="GET")
    with urlopen(req, context=context, timeout=15) as resp:  # noqa: S310 - fixed scheme
        body = resp.read().decode("utf-8")
    return json.loads(body)
