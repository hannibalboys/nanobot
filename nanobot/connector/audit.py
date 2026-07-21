"""Audit logging for connector file access (task 3.4).

Every connector file operation appends one JSON line to
``<workspace>/connector/audit.log`` and emits a structured log record. The audit
trail answers "who read which file on which device, when, and how big".
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from loguru import logger


def audit_file_access(
    workspace_path: Path,
    *,
    session: str | None,
    node_id: str,
    method: str,
    path: str,
    bytes_count: int = 0,
    result: str = "ok",
) -> None:
    """Append one audit record for a connector file access."""
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "session": session or "",
        "nodeId": node_id,
        "method": method,
        "path": path,
        "bytes": bytes_count,
        "result": result,
    }
    logger.info(
        "connector audit: session={} node={} method={} path={} bytes={} result={}",
        record["session"], node_id, method, path, bytes_count, result,
    )
    audit_path = Path(workspace_path) / "connector" / "audit.log"
    try:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        with open(audit_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning("connector audit write failed: {}", exc)
