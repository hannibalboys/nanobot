"""Shared fakes for connector hub integration tests."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from typing import Any

from nanobot.connector import protocol as proto


class FakeConn:
    """One end of an in-memory duplex; ``send`` writes to the peer's inbox."""

    def __init__(self) -> None:
        self._inbox: asyncio.Queue[str | None] = asyncio.Queue()
        self.peer: FakeConn | None = None
        self.sent: list[str] = []

    async def send(self, data: str) -> None:
        self.sent.append(data)
        assert self.peer is not None
        await self.peer._inbox.put(data)

    def __aiter__(self) -> "FakeConn":
        return self

    async def __anext__(self) -> str:
        item = await self._inbox.get()
        if item is None:
            raise StopAsyncIteration
        return item

    async def close(self) -> None:
        # Close both directions, like a real bidirectional WS close, so the peer's
        # read loop also terminates.
        await self._inbox.put(None)
        if self.peer is not None:
            await self.peer._inbox.put(None)


def duplex() -> tuple[FakeConn, FakeConn]:
    a, b = FakeConn(), FakeConn()
    a.peer, b.peer = b, a
    return a, b


class FakeConnector:
    """Minimal connector: registers, then answers fs.* rpc_requests from files."""

    def __init__(self, conn: FakeConn, *, files: dict[str, bytes], chunk_bytes: int = 8):
        self.conn = conn
        self.files = files
        self.chunk_bytes = chunk_bytes
        self.cancelled: set[str] = set()

    async def run(self, *, roots: list[str] | None = None, protocol: int = proto.PROTOCOL_VERSION):
        await self.conn.send(
            json.dumps(
                proto.dump_frame(
                    proto.RegisterFrame(
                        protocol=protocol,
                        node=proto.NodeInfo(name="fake", platform="test", roots=roots or []),
                    )
                )
            )
        )
        async for raw in self.conn:
            frame = proto.parse_frame(json.loads(raw))
            if isinstance(frame, proto.RegisteredFrame):
                continue
            if isinstance(frame, proto.CancelFrame):
                self.cancelled.add(frame.id)
                continue
            if isinstance(frame, proto.RpcRequestFrame):
                await self._handle_rpc(frame)

    async def _handle_rpc(self, frame: proto.RpcRequestFrame):
        path = frame.params.get("path", "")
        if frame.method == "fs.list":
            await self._respond(frame.id, {"entries": list(self.files)})
            return
        if frame.method == "fs.fetch":
            content = self.files.get(path)
            if content is None:
                await self._respond_error(frame.id, proto.ERROR_NOT_FOUND, "no such file")
                return
            await self._stream_file(frame.id, content)
            return
        await self._respond_error(frame.id, proto.ERROR_INTERNAL, "unknown method")

    async def _stream_file(self, rpc_id: str, content: bytes):
        seq = 0
        for i in range(0, len(content), self.chunk_bytes):
            if rpc_id in self.cancelled:
                return
            piece = content[i : i + self.chunk_bytes]
            await self.conn.send(
                json.dumps(
                    proto.dump_frame(
                        proto.FileChunkFrame(
                            id=rpc_id, seq=seq, data=base64.b64encode(piece).decode()
                        )
                    )
                )
            )
            seq += 1
            await asyncio.sleep(0.05)
        digest = hashlib.sha256(content).hexdigest()
        await self.conn.send(
            json.dumps(
                proto.dump_frame(
                    proto.FileChunkFrame(
                        id=rpc_id, seq=seq, data="", eof=True,
                        sha256=digest, total_bytes=len(content),
                    )
                )
            )
        )

    async def _respond(self, rpc_id: str, result: Any):
        await self.conn.send(
            json.dumps(proto.dump_frame(proto.RpcResponseFrame(id=rpc_id, ok=True, result=result)))
        )

    async def _respond_error(self, rpc_id: str, code: str, message: str):
        await self.conn.send(
            json.dumps(
                proto.dump_frame(
                    proto.RpcResponseFrame(id=rpc_id, ok=False, error={"code": code, "message": message})
                )
            )
        )
