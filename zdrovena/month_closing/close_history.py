"""Close history — append-only log of month-closing runs stored in blob storage."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("zdrovena.month_closing.history")

HISTORY_BLOB_KEY = "faktury/.close_history.jsonl"


def _get_table_connection() -> str | None:
    """Return Azure Storage connection string or account URL if configured."""
    return os.environ.get("AZURE_STORAGE_CONNECTION_STRING") or os.environ.get(
        "AZURE_STORAGE_ACCOUNT_URL"
    )


def append_close_history(storage: Any, entry: dict) -> None:
    """Append one entry to the history log.

    Tries Azure Table Storage first (if AZURE_STORAGE_CONNECTION_STRING or
    AZURE_STORAGE_ACCOUNT_URL is set), falls back to JSONL blob.

    Uses download → append → upload for JSONL to avoid concurrent write issues
    for this low-frequency operation (month-close runs at most once a month per year).
    """
    conn = _get_table_connection()
    if conn:
        from zdrovena.month_closing.table_history import append_history_table

        try:
            append_history_table(conn, entry)
            return
        except Exception as exc:
            logger.warning("Table Storage append failed, falling back to JSONL: %s", exc)

    try:
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
            tmp = Path(f.name)

        # Download existing history (if any)
        try:
            storage.download(HISTORY_BLOB_KEY, tmp)
            existing = tmp.read_text(encoding="utf-8")
        except Exception:
            existing = ""

        # Append new entry
        line = json.dumps(entry, ensure_ascii=False, default=str)
        new_content = (existing.rstrip("\n") + "\n" + line + "\n").lstrip("\n")
        tmp.write_text(new_content, encoding="utf-8")

        storage.upload(tmp, HISTORY_BLOB_KEY)
        tmp.unlink(missing_ok=True)
    except Exception as exc:
        logger.warning("Could not append close history: %s", exc)


def read_close_history(storage: Any, limit: int = 50) -> list[dict]:
    """Return last `limit` history entries, newest first.

    Tries Azure Table Storage first (if AZURE_STORAGE_CONNECTION_STRING or
    AZURE_STORAGE_ACCOUNT_URL is set), falls back to JSONL blob.
    """
    conn = _get_table_connection()
    if conn:
        from zdrovena.month_closing.table_history import read_history_table

        try:
            return read_history_table(conn, limit=limit)
        except Exception as exc:
            logger.warning("Table Storage read failed, falling back to JSONL: %s", exc)

    try:
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
            tmp = Path(f.name)
        storage.download(HISTORY_BLOB_KEY, tmp)
        lines = [l.strip() for l in tmp.read_text(encoding="utf-8").splitlines() if l.strip()]
        tmp.unlink(missing_ok=True)
        entries = []
        for line in reversed(lines[-limit:]):
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return entries
    except Exception:
        return []


def delete_history_entry(storage: Any, ts: str) -> bool:
    """Remove one entry by timestamp. Returns True if found and removed.

    Tries Azure Table Storage first (if AZURE_STORAGE_CONNECTION_STRING or
    AZURE_STORAGE_ACCOUNT_URL is set), falls back to JSONL blob.
    """
    conn = _get_table_connection()
    if conn:
        from zdrovena.month_closing.table_history import delete_history_entry_table

        try:
            return delete_history_entry_table(conn, ts)
        except Exception as exc:
            logger.warning("Table Storage delete failed, falling back to JSONL: %s", exc)

    try:
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
            tmp = Path(f.name)
        try:
            storage.download(HISTORY_BLOB_KEY, tmp)
            lines = [l.strip() for l in tmp.read_text(encoding="utf-8").splitlines() if l.strip()]
        except Exception:
            tmp.unlink(missing_ok=True)
            return False

        kept = []
        removed = 0
        for line in lines:
            try:
                entry = json.loads(line)
                if entry.get("ts") == ts:
                    removed += 1
                else:
                    kept.append(line)
            except json.JSONDecodeError:
                kept.append(line)

        new_content = "\n".join(kept) + ("\n" if kept else "")
        tmp.write_text(new_content, encoding="utf-8")
        storage.upload(tmp, HISTORY_BLOB_KEY)
        tmp.unlink(missing_ok=True)
        return removed > 0
    except Exception as exc:
        logger.warning("Could not delete history entry %s: %s", ts, exc)
        return False


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
        # Combine steps from this run + checkpoint steps (deduplicated, normalized)
        # This gives the accurate total of steps completed for this month.
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
