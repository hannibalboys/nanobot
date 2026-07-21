"""Tests for the client subprocess executor (v2 controlled execution)."""

from __future__ import annotations

import asyncio
import sys
import time

import pytest

from nanobot_connector.runner import run_execution


async def _collect(argv, **kwargs):
    chunks: list[tuple[str, str, int]] = []

    async def on_output(stream: str, text: str, seq: int) -> None:
        chunks.append((stream, text, seq))

    kwargs.setdefault("env_overlay", {})
    kwargs.setdefault("workdir", "")
    kwargs.setdefault("timeout_s", 10.0)
    kwargs.setdefault("max_output_bytes", 1_048_576)
    outcome = await run_execution(argv, on_output=on_output, **kwargs)
    return outcome, chunks


def _py(script: str) -> list[str]:
    return [sys.executable, "-c", script]


async def test_captures_stdout_and_exit_zero():
    outcome, chunks = await _collect(_py("print('hello world')"))
    assert outcome.exit_code == 0
    assert not outcome.timed_out and not outcome.cancelled and not outcome.truncated
    text = "".join(c[1] for c in chunks if c[0] == "stdout")
    assert "hello world" in text


async def test_captures_stderr():
    outcome, chunks = await _collect(
        _py("import sys; sys.stderr.write('boom'); sys.exit(3)")
    )
    assert outcome.exit_code == 3
    err = "".join(c[1] for c in chunks if c[0] == "stderr")
    assert "boom" in err


async def test_env_overlay_injected():
    outcome, chunks = await _collect(
        _py("import os; print(os.environ.get('SECRET_TOKEN', 'MISSING'))"),
        env_overlay={"SECRET_TOKEN": "xyz"},
    )
    assert outcome.exit_code == 0
    out = "".join(c[1] for c in chunks if c[0] == "stdout")
    assert "xyz" in out


async def test_timeout_kills_process():
    start = time.monotonic()
    outcome, _ = await _collect(_py("import time; time.sleep(30)"), timeout_s=0.5)
    elapsed = time.monotonic() - start
    assert outcome.timed_out is True
    assert outcome.exit_code != 0  # killed, not a clean 0
    assert elapsed < 10  # did not wait the full 30s


async def test_cancel_terminates():
    async def on_output(stream, text, seq):
        pass

    cancel = asyncio.Event()

    async def canceller():
        await asyncio.sleep(0.3)
        cancel.set()

    task = asyncio.create_task(canceller())
    start = time.monotonic()
    outcome = await run_execution(
        _py("import time; time.sleep(30)"),
        env_overlay={},
        workdir="",
        timeout_s=30.0,
        max_output_bytes=1_048_576,
        on_output=on_output,
        cancel_event=cancel,
    )
    await task
    assert outcome.cancelled is True
    assert time.monotonic() - start < 10


async def test_output_truncated_at_cap():
    # Print far more than the cap; executor must stop and flag truncation.
    outcome, chunks = await _collect(
        _py("print('A' * 100000)"),
        max_output_bytes=1000,
    )
    assert outcome.truncated is True
    total = sum(len(c[1]) for c in chunks)
    assert total <= 1000


async def test_missing_executable_raises_oserror():
    with pytest.raises(OSError):
        await _collect(["this-executable-does-not-exist-12345"])
