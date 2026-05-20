"""Close history — append-only log of month-closing runs.

Production: Azure Table Storage (via table_history.py, table 'closehistory')
  - Requires AZURE_STORAGE_ACCOUNT_URL or AZURE_STORAGE_CONNECTION_STRING
  - Write once per month-close; read for dashboard history panel

Local dev / tests: JSON file at ~/.zdrovena/storage/close-history.json
  - List of entry dicts, appended in order
  - No Azure credentials required
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("zdrovena.month_closing.history")

_LOCAL_FILE = Path.home() / ".zdrovena" / "storage" / "close-history.json"


def _get_conn() -> str | None:
    return os.environ.get("AZURE_STORAGE_CONNECTION_STRING") or os.environ.get(
        "AZURE_STORAGE_ACCOUNT_URL"
    )


# ── Local JSON fallback (dev / tests) ─────────────────────────────────────────


def _local_load() -> list[dict]:
    if not _LOCAL_FILE.exists():
        return []
    try:
        return json.loads(_LOCAL_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _local_save(entries: list[dict]) -> None:
    _LOCAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    _LOCAL_FILE.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")


# ── Public API ────────────────────────────────────────────────────────────────


def append_close_history(storage: Any, entry: dict) -> None:
    """Append one entry to the history log."""
    conn = _get_conn()
    if conn:
        from zdrovena.month_closing.table_history import append_history_table

        try:
            append_history_table(conn, entry)
            return
        except Exception as exc:
            logger.warning("Table Storage append failed: %s", exc)
            raise

    entries = _local_load()
    entries.append(entry)
    _local_save(entries)


def read_close_history(storage: Any, limit: int = 50) -> list[dict]:
    """Return last `limit` history entries, newest first."""
    conn = _get_conn()
    if conn:
        from zdrovena.month_closing.table_history import read_history_table

        try:
            return read_history_table(conn, limit=limit)
        except Exception as exc:
            logger.warning("Table Storage read failed: %s", exc)
            return []

    entries = _local_load()
    return list(reversed(entries[-limit:]))


def delete_history_entry(storage: Any, ts: str) -> bool:
    """Remove one entry by timestamp. Returns True if found and removed."""
    conn = _get_conn()
    if conn:
        from zdrovena.month_closing.table_history import delete_history_entry_table

        try:
            return delete_history_entry_table(conn, ts)
        except Exception as exc:
            logger.warning("Table Storage delete failed: %s", exc)
            return False

    entries = _local_load()
    kept = [e for e in entries if e.get("ts") != ts]
    if len(kept) == len(entries):
        return False
    _local_save(kept)
    return True


def build_history_entry(
    *,
    year: int,
    month: int,
    month_name: str,
    status: str,
    dry_run: bool,
    report: Any | None = None,
    error: str | None = None,
) -> dict:
    entry: dict = {
        "ts": datetime.now(tz=timezone.utc).isoformat(),
        "year": year,
        "month": month,
        "month_name": month_name,
        "status": status,
        "dry_run": dry_run,
    }
    if report is not None:
        entry["sales_invoice_count"] = report.sales_invoice_count
        entry["sales_gross_total"] = str(report.sales_gross_total)
        entry["cost_invoice_count"] = report.cost_invoice_count
        entry["warnings"] = report.warnings
        entry["errors"] = report.errors

        def _norm(s: str) -> str:
            import re

            return re.sub(r"\s*\(dry-run\)", "", s, flags=re.IGNORECASE)

        all_done = {_norm(s) for s in report.steps_completed}
        entry["steps_completed"] = len(all_done)
        entry["bank_statement_found"] = report.bank_statement_found
        entry["email_sent"] = report.email_sent
    if error:
        entry["error"] = error
    return entry
