"""Local audit log for connector file access (task 5.5).

Appends one JSON line per fs.* call to ``~/.nanobot-connector/logs/audit.log`` and
prunes records older than 30 days on startup.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from nanobot_connector.config import config_dir

_RETENTION_DAYS = 30


def audit_log_path() -> Path:
    return config_dir() / "logs" / "audit.log"


def record(method: str, path: str, *, result: str = "ok", bytes_count: int = 0) -> None:
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "method": method,
        "path": path,
        "result": result,
        "bytes": bytes_count,
    }
    p = audit_log_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def prune() -> None:
    """Drop audit lines older than the retention window."""
    p = audit_log_path()
    if not p.exists():
        return
    cutoff = time.time() - _RETENTION_DAYS * 86400
    keep: list[str] = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                ts = json.loads(line).get("ts", "")
                epoch = time.mktime(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))
            except (ValueError, OSError):
                keep.append(line)
                continue
            if epoch >= cutoff:
                keep.append(line)
        p.write_text("\n".join(keep) + ("\n" if keep else ""), encoding="utf-8")
    except OSError:
        pass
