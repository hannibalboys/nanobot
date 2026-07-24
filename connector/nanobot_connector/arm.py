"""On-device time-boxed pre-authorization ("arm") for controlled capabilities.

Desktop control and ``approval=local`` tools/MCP are fail-closed: the daemon can't
run them unless the device owner has consented on this machine. A headless service
can't pop an interactive prompt, so the owner pre-authorizes a category for a bounded
window from the CLI (``nanobot-connector arm desktop --for 30m``), mirroring
OpenClaw's ``/phone arm computer``.

State lives in ``~/.nanobot-connector/arm.json`` (mode 0600) so the separately-running
``arm`` CLI and the ``start`` daemon share it: the daemon re-reads the file at each
approval check, so arming/disarming takes effect immediately without a restart.
Entries carry an absolute expiry epoch; a missing or elapsed entry means *not armed*.
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path

from nanobot_connector.config import config_dir
from nanobot_connector.persistence import LocalStateError, locked_file, write_json_atomic

CATEGORIES = ("exec", "mcp", "desktop")


class ArmStore:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path or (config_dir() / "arm.json")

    def _load(self) -> dict[str, float]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise LocalStateError(f"本机授权状态文件不是有效 JSON：{self._path}") from exc
        if not isinstance(data, dict):
            raise LocalStateError(f"本机授权状态文件根节点必须是对象：{self._path}")
        out: dict[str, float] = {}
        for k, v in data.items():
            try:
                expiry = float(v)
            except (TypeError, ValueError):
                continue
            if math.isfinite(expiry):
                out[str(k)] = expiry
        return out

    def _save_unlocked(self, data: dict[str, float]) -> None:
        write_json_atomic(self._path, data)

    def _save(self, data: dict[str, float]) -> None:
        """Persist a test/admin supplied snapshot while respecting the local lock."""
        with locked_file(self._path):
            self._save_unlocked(data)

    def arm(self, category: str, ttl_s: int) -> float:
        """Arm *category* for ``ttl_s`` seconds. Returns the expiry epoch."""
        if category not in CATEGORIES:
            raise ValueError(f"unknown category: {category}")
        with locked_file(self._path):
            data = self._load()
            expiry = time.time() + max(1, ttl_s)
            data[category] = expiry
            self._save_unlocked(data)
        return expiry

    def disarm(self, category: str) -> None:
        """Disarm one category, or all when ``category == 'all'``."""
        with locked_file(self._path):
            if category == "all":
                self._save_unlocked({})
                return
            data = self._load()
            if data.pop(category, None) is not None:
                self._save_unlocked(data)

    def is_armed(self, category: str) -> bool:
        expiry = self._load().get(category)
        return expiry is not None and time.time() < expiry

    def remaining(self, category: str) -> int:
        """Seconds left on *category*'s arm window; 0 when not armed."""
        expiry = self._load().get(category)
        if expiry is None:
            return 0
        # ``int`` truncates a still-valid 0.1s window to zero, making the
        # display claim "not armed" while the call path would still accept it.
        return max(0, math.ceil(expiry - time.time()))

    def status(self) -> dict[str, int]:
        """Category -> remaining seconds (only currently-armed categories)."""
        now = time.time()
        out: dict[str, int] = {}
        for cat, expiry in self._load().items():
            remaining = math.ceil(expiry - now)
            if remaining > 0:
                out[cat] = remaining
        return out


def parse_duration(text: str) -> int:
    """Parse a human duration like ``30m`` / ``2h`` / ``90s`` / ``45`` into seconds."""
    text = (text or "").strip().lower()
    if not text:
        raise ValueError("empty duration")
    units = {"s": 1, "m": 60, "h": 3600}
    if text[-1] in units:
        return int(float(text[:-1]) * units[text[-1]])
    return int(float(text))
