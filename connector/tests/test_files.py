"""Tests for the connector client filesystem service (task 5.7)."""

from __future__ import annotations

import base64
import hashlib
import sys

import pytest

from nanobot_connector.files import (
    FetchChunker,
    FileService,
    NotFoundError,
    NotTextError,
    PathDeniedError,
    TooLargeError,
    is_filesystem_root,
    resolve_within_roots,
)


@pytest.fixture
def shared(tmp_path):
    root = tmp_path / "share"
    (root / "sub").mkdir(parents=True)
    (root / "a.txt").write_text("hello", encoding="utf-8")
    (root / "sub" / "b.txt").write_text("world", encoding="utf-8")
    return root


def test_resolve_allows_inside(shared):
    p = resolve_within_roots(str(shared / "a.txt"), [shared])
    assert p.name == "a.txt"


def test_resolve_denies_outside(shared, tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("x", encoding="utf-8")
    with pytest.raises(PathDeniedError):
        resolve_within_roots(str(outside), [shared])


def test_resolve_denies_traversal(shared):
    with pytest.raises(PathDeniedError):
        resolve_within_roots(str(shared / ".." / "etc"), [shared])


@pytest.mark.skipif(sys.platform == "win32", reason="symlink perms on Windows CI")
def test_resolve_denies_symlink_escape(shared, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "s.txt").write_text("secret", encoding="utf-8")
    link = shared / "escape"
    link.symlink_to(outside)
    with pytest.raises(PathDeniedError):
        resolve_within_roots(str(link / "s.txt"), [shared])


def test_no_roots_denies(shared):
    with pytest.raises(PathDeniedError):
        resolve_within_roots(str(shared / "a.txt"), [])


def test_is_filesystem_root():
    from pathlib import Path

    assert is_filesystem_root(Path(Path.cwd().anchor))
    assert not is_filesystem_root(Path.cwd())


def test_list_dir(shared):
    svc = FileService([shared])
    result = svc.list_dir(str(shared))
    names = {e["name"] for e in result["entries"]}
    assert names == {"a.txt", "sub"}


def test_read_text(shared):
    svc = FileService([shared])
    assert svc.read_text(str(shared / "a.txt"), max_bytes=100)["content"] == "hello"


def test_read_text_too_large(shared):
    svc = FileService([shared])
    with pytest.raises(TooLargeError):
        svc.read_text(str(shared / "a.txt"), max_bytes=1)


def test_read_binary_raises_not_text(shared):
    (shared / "img.bin").write_bytes(b"\xff\xfe\x00\x01")
    svc = FileService([shared])
    with pytest.raises(NotTextError):
        svc.read_text(str(shared / "img.bin"), max_bytes=100)


def test_read_missing(shared):
    svc = FileService([shared])
    with pytest.raises(NotFoundError):
        svc.read_text(str(shared / "nope.txt"), max_bytes=100)


def test_search(shared):
    svc = FileService([shared])
    result = svc.search("b.txt")
    found = {r["name"] for r in result["results"]}
    assert "b.txt" in found
    assert result["truncated"] is False


def test_fetch_chunker_integrity(shared):
    content = b"0123456789" * 100
    (shared / "big.bin").write_bytes(content)
    svc = FileService([shared])
    target, size = svc.open_for_fetch(str(shared / "big.bin"), max_file_bytes=10_000)
    assert size == len(content)

    frames = list(FetchChunker(target, "rpc1", chunk_bytes=64).frames())
    assert frames[-1]["eof"] is True
    assert frames[-1]["sha256"] == hashlib.sha256(content).hexdigest()
    assert frames[-1]["totalBytes"] == len(content)
    body = b"".join(base64.b64decode(f["data"]) for f in frames if not f["eof"])
    assert body == content
