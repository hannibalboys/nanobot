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
from nanobot_connector.config import ConnectorClientConfig, ConnectorConfigConflictError


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
    try:
        port_number = parts.port
    except ValueError as exc:
        raise PairingError("网关地址中的端口无效") from exc
    port = f":{port_number}" if port_number else ""
    path = parts.path.rstrip("/")
    return f"{scheme}://{host}{port}{path}"


def _validate_server(server: str) -> None:
    parts = urlsplit(server)
    if parts.scheme.lower() not in {"ws", "wss", "http", "https"} or not parts.hostname:
        raise PairingError("网关地址必须是包含主机名的 ws、wss、http 或 https 地址")
    try:
        _ = parts.port
    except ValueError as exc:
        raise PairingError("网关地址中的端口无效") from exc
    if parts.username or parts.password or parts.query or parts.fragment:
        raise PairingError("网关地址不能包含账号、查询参数或片段")
    if parts.path.rstrip("/"):
        raise PairingError("网关地址不能包含路径，请只填写网关根地址")


def _normalize_fingerprint(fingerprint: str) -> str:
    normalized = fingerprint.replace(":", "").strip().lower()
    if normalized and (len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized)):
        raise PairingError("证书指纹必须是 64 位 SHA-256 十六进制值")
    return normalized


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
    _validate_server(server)

    server_changed = bool(cfg.server and normalize_server_url(cfg.server) != normalize_server_url(server))
    if server_changed and not replace_server:
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
    supplied_fingerprint = _normalize_fingerprint(fingerprint)
    # Re-pairing the same self-signed gateway must retain the verified pin when
    # the caller does not explicitly replace it. A server migration never carries
    # a pin across hosts, because that would weaken endpoint identity checks.
    candidate.cert_fingerprint = supplied_fingerprint or ("" if server_changed else cfg.cert_fingerprint)
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
    try:
        candidate.save()
    except ConnectorConfigConflictError as exc:
        raise PairingError("本机连接器配置已被其他进程修改；请重新打开后再次配对") from exc
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
    try:
        certificate = sock.getpeercert(binary_form=True)
    except OSError as exc:
        raise PairingError("无法读取服务器证书，不能验证证书指纹") from exc
    if not certificate:
        raise PairingError("服务器未返回可验证的证书")
    actual = hashlib.sha256(certificate).hexdigest()
    if not hmac.compare_digest(actual, expected.replace(":", "").lower()):
        raise PairingError("服务器证书指纹不匹配")
