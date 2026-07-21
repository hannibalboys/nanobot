"""Filesystem service: allow-list enforcement and fs.* handlers.

The connector treats every server-supplied path as untrusted. ``resolve_within_roots``
resolves symlinks and ``..`` then requires the result to sit inside a user-declared
shared root; anything else is ``path_denied``. v1 is read-only: list/search/read/stat/fetch.
"""

from __future__ import annotations

import base64
import hashlib
import os
import time
from dataclasses import dataclass
from pathlib import Path

from nanobot_connector import protocol as proto

# fs.search resource ceilings (protect the client from runaway scans).
SEARCH_MAX_RESULTS = 50
SEARCH_MAX_SECONDS = 10.0
SEARCH_MAX_DEPTH = 12


class PathDeniedError(Exception):
    pass


class NotFoundError(Exception):
    pass


class NotTextError(Exception):
    pass


class TooLargeError(Exception):
    pass


def normalize_roots(raw_roots: list[str]) -> list[Path]:
    roots: list[Path] = []
    for r in raw_roots:
        p = Path(r).expanduser().resolve()
        roots.append(p)
    return roots


def is_filesystem_root(path: Path) -> bool:
    resolved = path.expanduser().resolve()
    return resolved == Path(resolved.anchor)


def resolve_within_roots(raw: str, roots: list[Path]) -> Path:
    """Resolve *raw* and require it to live inside one of *roots*."""
    if not roots:
        raise PathDeniedError("no shared folders configured")
    p = Path(raw).expanduser().resolve()
    for root in roots:
        if p == root or root in p.parents:
            return p
    raise PathDeniedError(f"path outside shared folders: {raw}")


class FileService:
    def __init__(self, roots: list[Path]):
        self.roots = roots

    # -- read-only handlers -------------------------------------------------

    def list_dir(self, path: str, max_entries: int = 500) -> dict:
        target = resolve_within_roots(path, self.roots)
        if not target.exists():
            raise NotFoundError(path)
        if not target.is_dir():
            raise PathDeniedError("not a directory")
        entries = []
        for i, child in enumerate(sorted(target.iterdir())):
            if i >= max_entries:
                break
            try:
                st = child.stat()
                entries.append({
                    "name": child.name,
                    "type": "dir" if child.is_dir() else "file",
                    "size": st.st_size,
                    "mtime": int(st.st_mtime),
                })
            except OSError:
                continue
        return {"entries": entries, "path": str(target)}

    def stat(self, path: str) -> dict:
        target = resolve_within_roots(path, self.roots)
        if not target.exists():
            raise NotFoundError(path)
        st = target.stat()
        return {
            "path": str(target),
            "type": "dir" if target.is_dir() else "file",
            "size": st.st_size,
            "mtime": int(st.st_mtime),
        }

    def search(self, query: str, path: str = "") -> dict:
        scopes = [resolve_within_roots(path, self.roots)] if path else self.roots
        needle = query.lower()
        results: list[dict] = []
        deadline = time.monotonic() + SEARCH_MAX_SECONDS
        truncated = False
        for scope in scopes:
            base_depth = len(scope.parts)
            for root, dirs, files in os.walk(scope):
                if time.monotonic() > deadline:
                    truncated = True
                    break
                depth = len(Path(root).parts) - base_depth
                if depth >= SEARCH_MAX_DEPTH:
                    dirs[:] = []
                    continue
                for name in files:
                    if needle in name.lower():
                        results.append({"path": str(Path(root) / name), "name": name})
                        if len(results) >= SEARCH_MAX_RESULTS:
                            truncated = True
                            break
                if truncated:
                    break
            if truncated:
                break
        return {"results": results, "truncated": truncated}

    def read_text(self, path: str, max_bytes: int) -> dict:
        target = resolve_within_roots(path, self.roots)
        if not target.exists() or not target.is_file():
            raise NotFoundError(path)
        if target.stat().st_size > max_bytes:
            raise TooLargeError(path)
        raw = target.read_bytes()
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise NotTextError(path) from exc
        return {"content": content, "path": str(target)}

    def open_for_fetch(self, path: str, max_file_bytes: int) -> tuple[Path, int]:
        target = resolve_within_roots(path, self.roots)
        if not target.exists() or not target.is_file():
            raise NotFoundError(path)
        size = target.stat().st_size
        if size > max_file_bytes:
            raise TooLargeError(path)
        return target, size


@dataclass
class FetchChunker:
    """Yield base64 file_chunk frames for a file, tail carries sha256.

    ``total_bytes`` (the size known at open time) rides on every data frame so
    the server can reserve cache quota before the first byte lands.
    """

    path: Path
    rpc_id: str
    chunk_bytes: int
    total_bytes: int | None = None

    def frames(self):
        hasher = hashlib.sha256()
        total = 0
        seq = 0
        with open(self.path, "rb") as fh:
            while True:
                block = fh.read(self.chunk_bytes)
                if not block:
                    break
                hasher.update(block)
                total += len(block)
                yield proto.file_chunk(
                    self.rpc_id, seq, base64.b64encode(block).decode(), self.total_bytes
                )
                seq += 1
        yield proto.file_chunk_eof(self.rpc_id, seq, hasher.hexdigest(), total)
