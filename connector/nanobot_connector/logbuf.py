"""In-process event log for the connector GUI's 运行日志 tab.

A tiny ring buffer the daemon client writes connection/protocol events into via
:func:`log_event`; the GUI drains it once a second. Kept dependency-free and
thread-safe: the client runs on its own asyncio thread while the GUI polls from
the tkinter main thread.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass

_CAPACITY = 500


@dataclass(frozen=True)
class LogEvent:
    ts: float
    level: str  # "info" | "warn" | "error"
    message: str


_lock = threading.Lock()
_events: deque[LogEvent] = deque(maxlen=_CAPACITY)


def log_event(message: str, level: str = "info") -> None:
    """Append an event; also mirrors to stdout so CLI ``start`` keeps its output."""
    with _lock:
        _events.append(LogEvent(time.time(), level, message))
    print(message)


def snapshot() -> list[LogEvent]:
    """Return a copy of all buffered events, oldest first."""
    with _lock:
        return list(_events)


def clear() -> None:
    with _lock:
        _events.clear()
