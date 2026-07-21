"""Integration tests for ConnectorHub + transfer (task 3.5)."""

from __future__ import annotations

import asyncio

import pytest

from nanobot.connector import protocol as proto
from nanobot.connector.hub import (
    ConnectorDisconnectedError,
    ConnectorHub,
    ConnectorRemoteError,
)
from nanobot.connector.transfer import TransferError, sanitize_landing_path

from .conftest import FakeConnector, duplex


async def _online_hub(files, *, chunk_bytes=8):
    """Start a hub serving a fake connector; return (hub, tasks, connector, sconn)."""
    server_conn, client_conn = duplex()
    hub = ConnectorHub()
    connector = FakeConnector(client_conn, files=files, chunk_bytes=chunk_bytes)
    serve_task = asyncio.create_task(
        hub.serve(server_conn, node_id="dev-1", owner_id="webui:xu")
    )
    connector_task = asyncio.create_task(connector.run())
    for _ in range(100):
        if hub.list_nodes():
            break
        await asyncio.sleep(0.01)
    return hub, serve_task, connector_task, connector, server_conn


async def test_register_and_list_nodes():
    hub, serve_task, ctask, _c, sconn = await _online_hub({})
    nodes = hub.list_nodes()
    assert len(nodes) == 1
    assert nodes[0]["nodeId"] == "dev-1"
    assert nodes[0]["ownerId"] == "webui:xu"
    await sconn.close()
    await asyncio.gather(serve_task, ctask)


async def test_rpc_roundtrip():
    hub, serve_task, ctask, _c, sconn = await _online_hub({"a.txt": b"x"})
    result = await hub.rpc("dev-1", "fs.list", {"path": "/"}, timeout=2)
    assert result == {"entries": ["a.txt"]}
    await sconn.close()
    await asyncio.gather(serve_task, ctask)


async def test_fetch_success(tmp_path):
    content = b"hello connector world" * 10
    hub, serve_task, ctask, _c, sconn = await _online_hub({"D:/x/a.bin": content})
    dest = await hub.fetch_file(
        "dev-1", "D:/x/a.bin",
        base_dir=tmp_path, max_file_bytes=10_000,
        fetch_cache_max_bytes=10_000, timeout=3,
    )
    assert dest.read_bytes() == content
    assert dest.parent.name == "x"
    assert "dev-1" in str(dest)
    assert not list(tmp_path.rglob("*.part"))
    await sconn.close()
    await asyncio.gather(serve_task, ctask)


async def test_fetch_not_found_maps_error(tmp_path):
    hub, serve_task, ctask, _c, sconn = await _online_hub({})
    with pytest.raises(ConnectorRemoteError) as ei:
        await hub.fetch_file(
            "dev-1", "missing.txt",
            base_dir=tmp_path, max_file_bytes=10_000,
            fetch_cache_max_bytes=10_000, timeout=3,
        )
    assert ei.value.code == proto.ERROR_NOT_FOUND
    await sconn.close()
    await asyncio.gather(serve_task, ctask)


async def test_fetch_too_large_aborts(tmp_path):
    hub, serve_task, ctask, _c, sconn = await _online_hub({"big.bin": b"y" * 500})
    with pytest.raises(Exception) as ei:
        await hub.fetch_file(
            "dev-1", "big.bin",
            base_dir=tmp_path, max_file_bytes=100,
            fetch_cache_max_bytes=10_000, timeout=3,
        )
    assert getattr(ei.value, "code", "") == proto.ERROR_TOO_LARGE
    assert not list(tmp_path.rglob("*.part"))
    await sconn.close()
    await asyncio.gather(serve_task, ctask)


async def test_disconnect_fails_pending_rpc(tmp_path):
    hub, serve_task, ctask, _c, sconn = await _online_hub({})
    await sconn.close()
    await serve_task
    with pytest.raises(ConnectorDisconnectedError):
        await hub.rpc("dev-1", "fs.list", {}, timeout=1)
    ctask.cancel()


async def test_owner_isolation(tmp_path):
    hub, serve_task, ctask, _c, sconn = await _online_hub({"a.txt": b"x"})
    assert hub.list_nodes(owner_id="webui:other") == []
    with pytest.raises(ConnectorDisconnectedError):
        await hub.rpc("dev-1", "fs.list", {}, timeout=1, owner_id="webui:other")
    await sconn.close()
    await asyncio.gather(serve_task, ctask)


def test_path_sanitize_blocks_escape(tmp_path):
    with pytest.raises(TransferError):
        sanitize_landing_path(tmp_path, "dev-1", "../../../etc/passwd")


def test_path_sanitize_windows_drive(tmp_path):
    dest = sanitize_landing_path(tmp_path, "dev-1", r"D:\PPT\report.docx")
    assert dest.parent.name == "PPT"
    assert (tmp_path / "dev-1").resolve() in dest.parents


async def test_fetch_cancel_on_timeout(tmp_path):
    """Slow stream should be cancelled when transfer times out (task 3.5)."""
    content = b"x" * 64
    hub, serve_task, ctask, connector, sconn = await _online_hub(
        {"slow.bin": content}, chunk_bytes=4
    )

    async def slow_fetch():
        return await hub.fetch_file(
            "dev-1", "slow.bin",
            base_dir=tmp_path, max_file_bytes=10_000,
            fetch_cache_max_bytes=10_000, timeout=0.05,
        )

    with pytest.raises(Exception) as ei:
        await slow_fetch()
    assert getattr(ei.value, "code", "") == proto.ERROR_RPC_TIMEOUT
    assert any("cancel" in raw for raw in sconn.sent)
    await sconn.close()
    await asyncio.gather(serve_task, ctask)


async def test_lru_cache_eviction(tmp_path):
    landing = tmp_path / "connector"
    landing.mkdir()
    old = landing / "dev-1" / "old"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"old" * 100)

    hub, serve_task, ctask, _c, sconn = await _online_hub({"new.bin": b"n" * 200})
    await hub.fetch_file(
        "dev-1", "new.bin",
        base_dir=landing, max_file_bytes=10_000,
        fetch_cache_max_bytes=250, timeout=3,
    )
    assert not old.exists()
    await sconn.close()
    await asyncio.gather(serve_task, ctask)


async def test_concurrent_transfer_limit(tmp_path):
    hub, serve_task, ctask, _c, sconn = await _online_hub({"a.bin": b"a" * 100})
    node = hub._nodes["dev-1"]
    node._transfers["busy-1"] = type("T", (), {"queue": asyncio.Queue()})()

    with pytest.raises(Exception) as ei:
        await hub.fetch_file(
            "dev-1", "a.bin",
            base_dir=tmp_path, max_file_bytes=10_000,
            fetch_cache_max_bytes=10_000, timeout=3,
            max_concurrent_transfers=1,
        )
    assert "concurrent" in str(ei.value).lower()
    await sconn.close()
    await asyncio.gather(serve_task, ctask)


async def test_disconnect_revoked(tmp_path):
    hub, serve_task, ctask, connector, sconn = await _online_hub({})
    assert await hub.disconnect_node("dev-1", revoked=True) is True
    assert hub.list_nodes() == []
    assert any("revoked" in raw for raw in sconn.sent)
    await asyncio.gather(serve_task, ctask)


async def test_reconnect_supersede_keeps_new_connection():
    """The superseded old connection's cleanup must not drop the new node."""
    files = {"a.txt": b"x"}
    server1, client1 = duplex()
    hub = ConnectorHub()
    c1 = FakeConnector(client1, files=files)
    serve1 = asyncio.create_task(hub.serve(server1, node_id="dev-1", owner_id="webui:xu"))
    ctask1 = asyncio.create_task(c1.run())
    for _ in range(100):
        if hub.list_nodes():
            break
        await asyncio.sleep(0.01)

    # Same node reconnects on a fresh connection while the old one is live.
    server2, client2 = duplex()
    c2 = FakeConnector(client2, files=files)
    serve2 = asyncio.create_task(hub.serve(server2, node_id="dev-1", owner_id="webui:xu"))
    ctask2 = asyncio.create_task(c2.run())

    # Old serve loop must terminate (superseded) without detaching the new node.
    await asyncio.wait_for(serve1, timeout=2)
    ctask1.cancel()
    assert [n["nodeId"] for n in hub.list_nodes()] == ["dev-1"]

    # RPC must be routed over the *new* connection.
    result = await hub.rpc("dev-1", "fs.list", {"path": "/"}, timeout=2)
    assert result == {"entries": ["a.txt"]}

    await server2.close()
    await asyncio.gather(serve2, ctask2)
