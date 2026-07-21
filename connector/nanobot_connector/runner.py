"""Subprocess executor for controlled local tools (add-connector-local-tools).

Runs a validated ``argv`` (already rendered by :mod:`nanobot_connector.tools`) with
``shell=False`` so no value is ever shell-interpreted. Streams stdout/stderr through
a callback, enforces a wall-clock timeout and a total-output cap, supports external
cancellation, and always terminates the whole process *tree* (not just the direct
child) on timeout/cancel/truncation so no grandchild survives.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

_READ_CHUNK = 4096

# on_output(stream, text, seq) -> awaitable
OutputCallback = Callable[[str, str, int], Awaitable[None]]


@dataclass
class ExecOutcome:
    exit_code: int | None
    duration_ms: int
    timed_out: bool = False
    truncated: bool = False
    cancelled: bool = False


class _OutputCounter:
    """Enforces a shared byte budget across stdout+stderr, decoding to text."""

    def __init__(self, max_bytes: int) -> None:
        self._remaining = max_bytes
        self.truncated = False

    def take(self, chunk: bytes) -> tuple[str, bool]:
        """Return ``(decoded_text, capped)``; ``capped`` means the budget is spent."""
        if self._remaining <= 0:
            self.truncated = True
            return "", True
        if len(chunk) > self._remaining:
            chunk = chunk[: self._remaining]
            self.truncated = True
        self._remaining -= len(chunk)
        return chunk.decode("utf-8", errors="replace"), self.truncated


def _spawn_kwargs() -> dict:
    """Platform flags that put the child in its own group so we can kill the tree."""
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}  # POSIX: setsid → new process group


async def _terminate_tree(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/F", "/T", "/PID", str(proc.pid),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            with contextlib.suppress(Exception):
                await asyncio.wait_for(killer.wait(), timeout=5)
        else:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass
    with contextlib.suppress(Exception):
        await proc.wait()


async def run_execution(
    argv: list[str],
    *,
    env_overlay: dict[str, str],
    workdir: str,
    timeout_s: float,
    max_output_bytes: int,
    on_output: OutputCallback,
    cancel_event: asyncio.Event | None = None,
) -> ExecOutcome:
    """Run *argv* to completion (or timeout/cancel/truncation) and return the outcome.

    Raises ``OSError`` if the process cannot be started (e.g. missing executable);
    the caller maps that to a pre-start ``rpc_response(ok=false)``.
    """
    env = {**os.environ, **env_overlay}
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=workdir or None,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
        **_spawn_kwargs(),
    )
    counter = _OutputCounter(max_output_bytes)
    truncated_evt = asyncio.Event()
    start = time.monotonic()

    async def pump(stream: asyncio.StreamReader | None, name: str) -> None:
        if stream is None:
            return
        seq = 0
        while True:
            chunk = await stream.read(_READ_CHUNK)
            if not chunk:
                return
            text, capped = counter.take(chunk)
            if text:
                await on_output(name, text, seq)
                seq += 1
            if capped:
                truncated_evt.set()
                return

    pumps = [
        asyncio.create_task(pump(proc.stdout, "stdout")),
        asyncio.create_task(pump(proc.stderr, "stderr")),
    ]
    exit_task = asyncio.create_task(proc.wait())
    conds: list[asyncio.Task] = [asyncio.create_task(truncated_evt.wait())]
    if cancel_event is not None:
        conds.append(asyncio.create_task(cancel_event.wait()))

    timed_out = cancelled = False
    try:
        done, _pending = await asyncio.wait(
            [exit_task, *conds], timeout=timeout_s, return_when=asyncio.FIRST_COMPLETED
        )
        if not done:
            timed_out = True
        elif cancel_event is not None and cancel_event.is_set():
            cancelled = True
        # else: natural exit or truncation — both fall through to cleanup
    finally:
        for c in conds:
            c.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(*conds, return_exceptions=True)

    if proc.returncode is None:
        await _terminate_tree(proc)

    # Drain remaining buffered output (bounded by the same cap) then finish.
    with contextlib.suppress(Exception):
        await asyncio.gather(*pumps, return_exceptions=True)

    duration_ms = int((time.monotonic() - start) * 1000)
    return ExecOutcome(
        exit_code=proc.returncode,
        duration_ms=duration_ms,
        timed_out=timed_out,
        truncated=counter.truncated,
        cancelled=cancelled,
    )
