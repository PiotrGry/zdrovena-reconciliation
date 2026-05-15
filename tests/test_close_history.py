"""Tests for zdrovena.month_closing.close_history."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from zdrovena.month_closing.close_history import (
    append_close_history,
    build_history_entry,
    delete_history_entry,
    read_close_history,
)


def _make_storage(content: str = "") -> MagicMock:
    """Mock storage that reads/writes to a temp file."""
    tmp = Path(tempfile.mkstemp(suffix=".jsonl")[1])
    tmp.write_text(content, encoding="utf-8")

    storage = MagicMock()

    def download(key, path):
        path.write_text(tmp.read_text(encoding="utf-8"), encoding="utf-8")

    def upload(path, key):
        tmp.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")

    storage.download.side_effect = download
    storage.upload.side_effect = upload
    storage._tmp = tmp
    return storage


def _read_tmp(storage: MagicMock) -> list[dict]:
    lines = storage._tmp.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


# ── build_history_entry ──────────────────────────────────────────────────────


class TestBuildHistoryEntry:
    def test_basic_fields_always_present(self):
        entry = build_history_entry(
            year=2026,
            month=4,
            month_name="Kwiecień",
            status="success",
            dry_run=False,
        )
        assert entry["year"] == 2026
        assert entry["month"] == 4
        assert entry["month_name"] == "Kwiecień"
        assert entry["status"] == "success"
        assert entry["dry_run"] is False
        assert "ts" in entry

    def test_steps_completed_from_report(self):
        report = MagicMock()
        report.steps_completed = ["Pre-flight", "Folder structure", "Sales invoices"]
        report.warnings = []
        report.errors = []
        report.sales_invoice_count = 73
        report.sales_gross_total = "6161.65"
        report.cost_invoice_count = 12
        report.bank_statement_found = True
        report.email_sent = True

        entry = build_history_entry(
            year=2026,
            month=4,
            month_name="Kwiecień",
            status="success",
            dry_run=False,
            report=report,
        )
        assert entry["steps_completed"] == 3  # len of list, not the list itself
        assert entry["sales_invoice_count"] == 73
        assert entry["email_sent"] is True

    def test_no_report_means_no_step_count(self):
        entry = build_history_entry(
            year=2026,
            month=4,
            month_name="Kwiecień",
            status="blocked",
            dry_run=True,
            error="Missing files",
        )
        assert "steps_completed" not in entry
        assert entry["error"] == "Missing files"

    def test_partial_status_with_warnings(self):
        report = MagicMock()
        report.steps_completed = [
            "Pre-flight",
            "Folder structure",
            "Sales invoices",
            "JPK & VAT reports",
            "Cost invoices",
            "Bank statement check",
            "ZIP archive (dry-run)",
        ]
        report.warnings = ["Brak faktury kosztowej: PayU"]
        report.errors = []
        report.sales_invoice_count = 73
        report.sales_gross_total = "6161.65"
        report.cost_invoice_count = 6
        report.bank_statement_found = True
        report.email_sent = False

        entry = build_history_entry(
            year=2026,
            month=4,
            month_name="Kwiecień",
            status="partial",
            dry_run=False,
            report=report,
        )
        assert entry["steps_completed"] == 7
        assert entry["status"] == "partial"
        assert len(entry["warnings"]) == 1


# ── append_close_history ─────────────────────────────────────────────────────


class TestAppendCloseHistory:
    def test_appends_to_empty_file(self):
        storage = _make_storage("")
        entry = build_history_entry(
            year=2026,
            month=4,
            month_name="Kwiecień",
            status="success",
            dry_run=False,
        )
        append_close_history(storage, entry)
        records = _read_tmp(storage)
        assert len(records) == 1
        assert records[0]["year"] == 2026

    def test_appends_multiple_entries(self):
        storage = _make_storage("")
        for month in [1, 2, 3]:
            append_close_history(
                storage,
                build_history_entry(
                    year=2026,
                    month=month,
                    month_name=f"M{month}",
                    status="success",
                    dry_run=False,
                ),
            )
        records = _read_tmp(storage)
        assert len(records) == 3
        assert [r["month"] for r in records] == [1, 2, 3]

    def test_blob_failure_does_not_raise(self):
        storage = MagicMock()
        storage.download.side_effect = Exception("blob not found")
        storage.upload.side_effect = Exception("upload failed")
        # Must not raise — history is best-effort
        append_close_history(storage, {"ts": "x", "year": 2026})


# ── read_close_history ────────────────────────────────────────────────────────


class TestReadCloseHistory:
    def test_returns_newest_first(self):
        content = (
            "\n".join(
                [
                    json.dumps({"ts": "2026-01-01T00:00:00Z", "month": 1}),
                    json.dumps({"ts": "2026-02-01T00:00:00Z", "month": 2}),
                    json.dumps({"ts": "2026-03-01T00:00:00Z", "month": 3}),
                ]
            )
            + "\n"
        )
        storage = _make_storage(content)
        records = read_close_history(storage, limit=10)
        # Newest first: March, February, January
        assert records[0]["month"] == 3
        assert records[1]["month"] == 2
        assert records[2]["month"] == 1

    def test_respects_limit(self):
        content = (
            "\n".join(
                json.dumps({"ts": f"2026-{m:02d}-01T00:00:00Z", "month": m}) for m in range(1, 13)
            )
            + "\n"
        )
        storage = _make_storage(content)
        records = read_close_history(storage, limit=5)
        assert len(records) == 5
        assert records[0]["month"] == 12  # newest first

    def test_empty_blob_returns_empty_list(self):
        storage = MagicMock()
        storage.download.side_effect = Exception("not found")
        assert read_close_history(storage) == []

    def test_ignores_malformed_lines(self):
        content = '{"month": 1}\nNOT_JSON\n{"month": 3}\n'
        storage = _make_storage(content)
        records = read_close_history(storage)
        assert len(records) == 2


# ── delete_history_entry ──────────────────────────────────────────────────────


class TestDeleteHistoryEntry:
    def test_removes_correct_entry(self):
        content = (
            "\n".join(
                [
                    json.dumps({"ts": "2026-01-01", "month": 1}),
                    json.dumps({"ts": "2026-02-01", "month": 2}),
                    json.dumps({"ts": "2026-03-01", "month": 3}),
                ]
            )
            + "\n"
        )
        storage = _make_storage(content)
        result = delete_history_entry(storage, "2026-02-01")
        assert result is True
        records = _read_tmp(storage)
        assert len(records) == 2
        assert all(r["month"] != 2 for r in records)

    def test_returns_false_when_not_found(self):
        storage = _make_storage('{"ts": "2026-01-01", "month": 1}\n')
        result = delete_history_entry(storage, "NOT_EXIST")
        assert result is False

    def test_blob_failure_returns_false(self):
        storage = MagicMock()
        storage.download.side_effect = Exception("not found")
        result = delete_history_entry(storage, "x")
        assert result is False


# ── Table Storage success paths ───────────────────────────────────────────────

_ENTRY = dict(year=2026, month=4, month_name="Kwiecień", status="success", dry_run=False)


class TestTableStorageSuccessPaths:
    """Cover lines 36-42, 74-79, 108-113 — table storage used when conn is available."""

    def test_append_uses_table_when_connection_available(self, monkeypatch):
        from unittest.mock import patch

        storage = MagicMock()
        entry = build_history_entry(**_ENTRY)
        monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", "UseDevelopmentStorage=true")

        with patch("zdrovena.month_closing.table_history.append_history_table") as mock_tbl:
            append_close_history(storage, entry)

        mock_tbl.assert_called_once()
        storage.download.assert_not_called()

    def test_append_falls_back_to_jsonl_on_table_error(self, monkeypatch):
        from unittest.mock import patch

        storage = _make_storage("")
        entry = build_history_entry(**_ENTRY)
        monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", "UseDevelopmentStorage=true")

        with patch(
            "zdrovena.month_closing.table_history.append_history_table",
            side_effect=RuntimeError("table unavailable"),
        ):
            append_close_history(storage, entry)

        records = _read_tmp(storage)
        assert len(records) == 1
        assert records[0]["month"] == 4

    def test_read_uses_table_when_connection_available(self, monkeypatch):
        from unittest.mock import patch

        storage = MagicMock()
        fake_rows = [{"ts": "2026-04-01T00:00:00Z", "month": 4, "year": 2026}]
        monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", "UseDevelopmentStorage=true")

        with patch(
            "zdrovena.month_closing.table_history.read_history_table", return_value=fake_rows
        ) as mock_tbl:
            result = read_close_history(storage, limit=10)

        mock_tbl.assert_called_once()
        assert result == fake_rows
        storage.download.assert_not_called()

    def test_delete_uses_table_when_connection_available(self, monkeypatch):
        from unittest.mock import patch

        storage = MagicMock()
        monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", "UseDevelopmentStorage=true")

        with patch(
            "zdrovena.month_closing.table_history.delete_history_entry_table", return_value=True
        ) as mock_tbl:
            result = delete_history_entry(storage, "2026-04-01T00:00:00Z")

        mock_tbl.assert_called_once_with("UseDevelopmentStorage=true", "2026-04-01T00:00:00Z")
        assert result is True
        storage.download.assert_not_called()
