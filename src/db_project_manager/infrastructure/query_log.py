"""Query/response JSONL logging for MCP tools (Phase 19.1).

Dedicated sink separate from the app log: one JSON object per line in
``logs/mcp_queries.log``. The file rotates daily at midnight — loguru moves
the active file to a dated name (``mcp_queries.YYYY-MM-DD_HH-mm-ss.log``) and
starts a fresh one — and rotated files older than the configured retention
are removed automatically.

Every event carries the full SQL text and the full DB response (jsonb values
included — the adapter pre-serializes them to JSON-safe types), so the log
may contain data literals: it is written for the local operator and must
stay out of git (``logs/`` is ignored together with the rest of the dir).

Configure via ``config.yaml``:

    logging:
      log_queries: true            # off disables the sink entirely
      queries_retention_days: 14

Order dependency: ``configure_query_log`` must run AFTER
:func:`db_project_manager.infrastructure.logging_setup.configure` — that
call does ``logger.remove()`` and would drop this sink.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from loguru import logger

#: Bind marker routing records to the query-log sink only.
_QUERY_LOG_MARKER = "mcp_query_log"

#: Active sink file name inside logs_dir (dated copies appear next to it
#: after each midnight rotation).
QUERY_LOG_FILENAME = "mcp_queries.log"


def configure_query_log(
    logs_dir: str | Path,
    *,
    enabled: bool = True,
    retention_days: int = 14,
) -> None:
    """Add the JSONL query-log sink: daily rotation, retention in days.

    Args:
        logs_dir: directory for ``mcp_queries.log`` (created when missing).
        enabled: False — no sink (nothing is written by :func:`log_db_call`).
        retention_days: rotated (dated) files older than this are deleted.
    """
    if not enabled:
        return
    logs_path = Path(logs_dir)
    logs_path.mkdir(parents=True, exist_ok=True)
    logger.add(
        logs_path / QUERY_LOG_FILENAME,
        mode="a",
        encoding="utf-8",
        level="INFO",
        filter=lambda record: record["extra"].get(_QUERY_LOG_MARKER, False),
        format="{message}",
        rotation="00:00",
        retention=timedelta(days=retention_days),
    )


def log_db_call(event: dict[str, Any]) -> None:
    """Emit one query event as a JSON line (timestamp added when missing).

    Without a configured sink (``log_queries: false``) this is a no-op —
    the bound record matches no sink filter and is dropped.
    """
    payload = {"ts": datetime.now().isoformat(timespec="milliseconds"), **event}
    line = json.dumps(payload, ensure_ascii=False, default=str)
    logger.bind(**{_QUERY_LOG_MARKER: True}).info(line)
