"""Chunked file receive: path sanitize, sha256 verify, atomic landing, LRU quota.

This is the server-side landing zone for ``fs.fetch``. Every client-supplied path
is treated as untrusted (design decision D8): it is stripped of drive/root,
``..`` is collapsed, and the resolved destination MUST stay inside
``<base_dir>/<node_id>/`` — independent of the connector's own allow-list, so a
compromised connector still cannot write outside the landing zone.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from loguru import logger

_ILLEGAL_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')


class TransferError(Exception):
    """Base for transfer failures; ``code`` maps to a protocol error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _split_client_path(client_path: str) -> list[str]:
    """Split a Windows- or POSIX-style client path into safe path segments."""
    raw = client_path.replace("\\", "/")
    # Drop a Windows drive prefix (D:/...) but keep the drive letter as a folder
    # so files from different drives don't collide.
    win = PureWindowsPath(client_path)
    drive = win.drive.rstrip(":") if win.drive else ""

    parts: list[str] = []
    if drive:
        parts.append(drive)
    for segment in PurePosixPath(raw).parts:
        seg = segment.strip().strip("/")
        if seg == "..":
            # A traversal attempt is treated as hostile, not silently stripped.
            raise TransferError("path_denied", "path traversal segment rejected")
        if not seg or seg == "." or seg.endswith(":"):
            continue
        seg = _ILLEGAL_CHARS.sub("_", seg)
        if seg in ("", ".", ".."):
            continue
        parts.append(seg)
    return parts


def sanitize_landing_path(base_dir: Path, node_id: str, client_path: str) -> Path:
    """Resolve where a fetched file should land, enforcing containment.

    Raises :class:`TransferError` (``path_denied``) if the result would escape
    ``<base_dir>/<node_id>/``.
    """
    node_root = (Path(base_dir) / node_id).resolve()
    segments = _split_client_path(client_path)
    if not segments:
        raise TransferError("path_denied", "empty or unsafe client path")
    dest = node_root.joinpath(*segments).resolve()
    if dest != node_root and node_root not in dest.parents:
        raise TransferError("path_denied", "resolved path escapes landing zone")
    return dest


def _dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total


def enforce_cache_quota(base_dir: Path, incoming_bytes: int, max_bytes: int) -> None:
    """LRU-evict landed files until ``incoming_bytes`` fits within ``max_bytes``.

    Raises :class:`TransferError` (``too_large``) if a single file cannot fit even
    an empty cache.
    """
    base = Path(base_dir)
    if incoming_bytes > max_bytes:
        raise TransferError("too_large", "file exceeds total cache quota")
    if not base.exists():
        return
    current = _dir_size(base)
    if current + incoming_bytes <= max_bytes:
        return

    files: list[tuple[float, Path, int]] = []
    for root, _dirs, names in os.walk(base):
        for name in names:
            p = Path(root) / name
            if p.suffix == ".part":
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            files.append((st.st_atime, p, st.st_size))
    files.sort(key=lambda t: t[0])  # oldest access first

    for _atime, path, size in files:
        if current + incoming_bytes <= max_bytes:
            break
        try:
            path.unlink()
            current -= size
            logger.info("connector: LRU-evicted cached file {}", path)
        except OSError:
            continue


@dataclass
class ChunkAssembler:
    """Accumulate base64 ``file_chunk`` payloads and verify integrity.

    The assembler streams decoded bytes to a temporary ``.part`` file, tracks the
    running size against ``max_file_bytes``, and on the eof sentinel verifies the
    sha256 before atomically renaming into place.
    """

    dest: Path
    max_file_bytes: int
    _tmp: Path | None = None
    _fh: object | None = None
    _hasher: object | None = None
    _received: int = 0
    _expected_seq: int = 0

    def __post_init__(self) -> None:
        self.dest.parent.mkdir(parents=True, exist_ok=True)
        self._tmp = self.dest.with_name(f".{self.dest.name}.{uuid.uuid4().hex}.part")
        self._fh = open(self._tmp, "wb")
        self._hasher = hashlib.sha256()

    def add_chunk(self, seq: int, data_b64: str) -> None:
        if self._fh is None:
            raise TransferError("internal", "assembler already closed")
        if seq != self._expected_seq:
            self.abort()
            raise TransferError("internal", f"out-of-order chunk: got {seq}")
        self._expected_seq += 1
        if not data_b64:
            return
        try:
            chunk = base64.b64decode(data_b64, validate=True)
        except Exception as exc:  # noqa: BLE001 - normalize decode failures
            self.abort()
            raise TransferError("decode", "invalid base64 chunk") from exc
        self._received += len(chunk)
        if self._received > self.max_file_bytes:
            self.abort()
            raise TransferError("too_large", "file exceeds max_file_bytes")
        self._fh.write(chunk)  # type: ignore[attr-defined]
        self._hasher.update(chunk)  # type: ignore[attr-defined]

    def finalize(self, *, sha256: str | None, total_bytes: int | None) -> Path:
        if self._fh is None:
            raise TransferError("internal", "assembler already closed")
        self._fh.flush()  # type: ignore[attr-defined]
        os.fsync(self._fh.fileno())  # type: ignore[attr-defined]
        self._fh.close()  # type: ignore[attr-defined]
        self._fh = None

        digest = self._hasher.hexdigest()  # type: ignore[attr-defined]
        if sha256 is not None and not _consteq(digest, sha256):
            self._unlink_tmp()
            raise TransferError("decode", "sha256 mismatch")
        if total_bytes is not None and total_bytes != self._received:
            self._unlink_tmp()
            raise TransferError("decode", "byte count mismatch")

        assert self._tmp is not None
        os.replace(self._tmp, self.dest)
        self._tmp = None
        return self.dest

    def abort(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()  # type: ignore[attr-defined]
            except OSError:
                pass
            self._fh = None
        self._unlink_tmp()

    def _unlink_tmp(self) -> None:
        if self._tmp is not None:
            try:
                self._tmp.unlink(missing_ok=True)
            except OSError:
                pass
            self._tmp = None

    @property
    def received_bytes(self) -> int:
        return self._received


def _consteq(a: str, b: str) -> bool:
    import hmac

    return hmac.compare_digest(a.lower(), b.lower())
