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
_PROCESS_POLL_S = 0.1
# A spawned GUI/browser process can inherit the tool parent's stdout/stderr handles.
# Once the direct tool process exits, waiting indefinitely for EOF on those handles
# would keep the connector RPC open until the spawned application exits.
_POST_EXIT_DRAIN_S = 1.0

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
    timed_out = cancelled = False
    deadline = start + timeout_s
    # ``asyncio.subprocess.Process.wait()`` also waits for stdout/stderr pipe
    # transports to close. A GUI/browser grandchild may inherit those handles,
    # so Process.wait() can remain pending after the direct tool process exits.
    # ``returncode`` is set when that direct process exits, independently of
    # the pipe lifetime; poll it together with cancellation and truncation.
    while proc.returncode is None:
        if truncated_evt.is_set():
            break
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            break
        await asyncio.sleep(min(_PROCESS_POLL_S, remaining))

    if proc.returncode is None:
        await _terminate_tree(proc)

    # Read output that was already buffered, but never wait forever for EOF. On
    # Windows in particular, GUI/browser tools often spawn a detached child that
    # inherits these pipe handles. The direct process has then completed, yet the
    # stream readers stay open until the GUI closes; awaiting them indefinitely
    # leaves the server waiting for ``exec_result`` and makes the chat look stuck.
    _done, pending_pumps = await asyncio.wait(pumps, timeout=_POST_EXIT_DRAIN_S)
    for pump_task in pending_pumps:
        pump_task.cancel()
    await asyncio.gather(*pumps, return_exceptions=True)

    duration_ms = int((time.monotonic() - start) * 1000)
    return ExecOutcome(
        exit_code=proc.returncode,
        duration_ms=duration_ms,
        timed_out=timed_out,
        truncated=counter.truncated,
        cancelled=cancelled,
    )


async def launch_execution(
    argv: list[str],
    *,
    env_overlay: dict[str, str],
    workdir: str,
) -> ExecOutcome:
    """Start a registered long-running GUI tool and return after it launches.

    This is intentionally separate from :func:`run_execution`: it is only used
    for an owner-declared ``completion=launch`` tool such as a browser or chat
    client. Its output is discarded and cancellation cannot terminate it after
    this function returns, so ordinary commands must keep the default
    ``completion=wait`` behavior.
    """
    start = time.monotonic()
    env = {**os.environ, **env_overlay}
    flags = _spawn_kwargs()
    if os.name == "nt":
        flags["creationflags"] |= subprocess.DETACHED_PROCESS
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=workdir or None,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        **flags,
    )
    # Yield once so an immediately failing executable can expose its exit code,
    # while a correctly launched GUI process is never waited on.
    await asyncio.sleep(0)
    return ExecOutcome(
        exit_code=proc.returncode if proc.returncode is not None else 0,
        duration_ms=int((time.monotonic() - start) * 1000),
    )
