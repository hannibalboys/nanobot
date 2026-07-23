"""HTTP pairing helpers shared by the CLI and GUI."""

from __future__ import annotations

import hashlib
import hmac
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


def normalize_server_url(server: str) -> str:
    """Canonicalize the server identity used for an explicit re-pair check."""
    parts = urlsplit(server.strip())
    if not parts.scheme or not parts.netloc:
        return server.strip().rstrip("/")
    scheme = parts.scheme.lower()
    host = parts.hostname.lower() if parts.hostname else ""
    port = f":{parts.port}" if parts.port else ""
    path = parts.path.rstrip("/")
    return f"{scheme}://{host}{port}{path}"


def pair_device(
    cfg: ConnectorClientConfig,
    *,
    server: str,
    code: str,
    name: str = "",
    fingerprint: str = "",
    insecure: bool = False,
    replace_server: bool = False,
) -> ConnectorClientConfig:
    """Redeem a pairing code and atomically replace the local device identity."""
    server = server.strip()
    code = normalize_pairing_code(code)
    if not server:
        raise PairingError("请填写网关地址")
    if not code:
        raise PairingError("请填写配对码")

    if cfg.server and normalize_server_url(cfg.server) != normalize_server_url(server) and not replace_server:
        raise PairingError(
            "目标服务器与当前已配对服务器不同。请确认迁移后使用 --replace-server 重试。"
        )

    # Never mutate ``cfg`` until the remote server and TLS identity have been
    # verified. A failed migration must leave the current connector usable.
    candidate = cfg.model_copy(deep=True)
    candidate.server = server
    # Default the device name to the machine hostname so the WebUI list shows a
    # readable name without anyone setting an alias.
    candidate.name = name.strip() or platform.node() or ""
    candidate.cert_fingerprint = fingerprint.replace(":", "").lower()
    candidate.insecure = insecure
    fp = machine_fingerprint()
    candidate.fingerprint = fp

    parts = urlsplit(server)
    scheme = "https" if parts.scheme in ("wss", "https") else "http"
    query = urlencode({
        "code": code,
        "name": candidate.name or "",
        "platform": platform.system().lower(),
        "fingerprint": fp,
    })
    url = f"{scheme}://{parts.netloc}/connector/pair?{query}"
    try:
        payload = _http_get_json(url, candidate)
    except Exception as exc:  # noqa: BLE001 - user-facing message
        hint = ""
        if scheme == "https" and not cfg.cert_fingerprint and not cfg.insecure:
            hint = " 若服务器使用自签证书，请联系管理员获取证书指纹。"
        raise PairingError(f"配对失败：{exc}{hint}") from exc

    try:
        candidate.node_id = str(payload["nodeId"])
        candidate.device_token = str(payload["token"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PairingError("配对服务器返回了无效的设备身份") from exc
    candidate.save()
    for field_name in type(cfg).model_fields:
        setattr(cfg, field_name, getattr(candidate, field_name))
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
        if cfg.cert_fingerprint and not cfg.insecure:
            _verify_certificate_fingerprint(resp, cfg.cert_fingerprint)
        body = resp.read().decode("utf-8")
    return json.loads(body)


def _verify_certificate_fingerprint(response, expected: str) -> None:
    """Verify a SHA-256 certificate fingerprint when normal CA validation is bypassed."""
    raw = getattr(getattr(response, "fp", None), "raw", None)
    sock = getattr(raw, "_sock", None)
    if sock is None:
        raise PairingError("无法读取服务器证书，不能验证证书指纹")
    certificate = sock.getpeercert(binary_form=True)
    actual = hashlib.sha256(certificate).hexdigest()
    if not hmac.compare_digest(actual, expected.replace(":", "").lower()):
        raise PairingError("服务器证书指纹不匹配")
