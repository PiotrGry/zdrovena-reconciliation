"""Close history — append-only log of month-closing runs stored in blob storage."""

from __future__ import annotations

import json
import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("zdrovena.month_closing.history")

HISTORY_BLOB_KEY = "faktury/.close_history.jsonl"


def append_close_history(storage: Any, entry: dict) -> None:
    """Append one entry to the history log in blob storage.

    Uses download → append → upload to avoid concurrent write issues for this
    low-frequency operation (month-close runs at most once a month per year).
    """
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
    """Return last `limit` history entries, newest first."""
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
        entry["steps_completed"] = len(report.steps_completed)
        entry["bank_statement_found"] = report.bank_statement_found
        entry["email_sent"] = report.email_sent
    if error:
        entry["error"] = error
    return entry
