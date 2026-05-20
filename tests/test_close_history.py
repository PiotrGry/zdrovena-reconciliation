"""Tests for zdrovena.month_closing.close_history."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import zdrovena.month_closing.close_history as history_mod
from zdrovena.month_closing.close_history import (
    append_close_history,
    build_history_entry,
    delete_history_entry,
    read_close_history,
)


def _write_local(local_file: Path, entries: list[dict]) -> None:
    local_file.parent.mkdir(parents=True, exist_ok=True)
    local_file.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")


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
        assert entry["steps_completed"] == 3
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
    def test_appends_to_empty_file(self, monkeypatch, tmp_path):
        local = tmp_path / "history.json"
        monkeypatch.setattr(history_mod, "_LOCAL_FILE", local)
        entry = build_history_entry(
            year=2026,
            month=4,
            month_name="Kwiecień",
            status="success",
            dry_run=False,
        )
        append_close_history(None, entry)
        records = json.loads(local.read_text())
        assert len(records) == 1
        assert records[0]["year"] == 2026

    def test_appends_multiple_entries(self, monkeypatch, tmp_path):
        local = tmp_path / "history.json"
        monkeypatch.setattr(history_mod, "_LOCAL_FILE", local)
        for month in [1, 2, 3]:
            append_close_history(
                None,
                build_history_entry(
                    year=2026,
                    month=month,
                    month_name=f"M{month}",
                    status="success",
                    dry_run=False,
                ),
            )
        records = json.loads(local.read_text())
        assert len(records) == 3
        assert [r["month"] for r in records] == [1, 2, 3]

    def test_creates_parent_dirs(self, monkeypatch, tmp_path):
        local = tmp_path / "deep" / "nested" / "history.json"
        monkeypatch.setattr(history_mod, "_LOCAL_FILE", local)
        append_close_history(None, {"ts": "x", "year": 2026})
        assert local.exists()


# ── read_close_history ────────────────────────────────────────────────────────


class TestReadCloseHistory:
    def test_returns_newest_first(self, monkeypatch, tmp_path):
        local = tmp_path / "history.json"
        monkeypatch.setattr(history_mod, "_LOCAL_FILE", local)
        entries = [
            {"ts": "2026-01-01T00:00:00Z", "month": 1},
            {"ts": "2026-02-01T00:00:00Z", "month": 2},
            {"ts": "2026-03-01T00:00:00Z", "month": 3},
        ]
        _write_local(local, entries)
        records = read_close_history(None, limit=10)
        assert records[0]["month"] == 3
        assert records[1]["month"] == 2
        assert records[2]["month"] == 1

    def test_respects_limit(self, monkeypatch, tmp_path):
        local = tmp_path / "history.json"
        monkeypatch.setattr(history_mod, "_LOCAL_FILE", local)
        entries = [{"ts": f"2026-{m:02d}-01T00:00:00Z", "month": m} for m in range(1, 13)]
        _write_local(local, entries)
        records = read_close_history(None, limit=5)
        assert len(records) == 5
        assert records[0]["month"] == 12  # newest first

    def test_missing_file_returns_empty_list(self, monkeypatch, tmp_path):
        local = tmp_path / "history.json"
        monkeypatch.setattr(history_mod, "_LOCAL_FILE", local)
        assert read_close_history(None) == []

    def test_malformed_file_returns_empty_list(self, monkeypatch, tmp_path):
        local = tmp_path / "history.json"
        monkeypatch.setattr(history_mod, "_LOCAL_FILE", local)
        local.write_text("NOT_VALID_JSON", encoding="utf-8")
        assert read_close_history(None) == []


# ── delete_history_entry ──────────────────────────────────────────────────────


class TestDeleteHistoryEntry:
    def test_removes_correct_entry(self, monkeypatch, tmp_path):
        local = tmp_path / "history.json"
        monkeypatch.setattr(history_mod, "_LOCAL_FILE", local)
        entries = [
            {"ts": "2026-01-01", "month": 1},
            {"ts": "2026-02-01", "month": 2},
            {"ts": "2026-03-01", "month": 3},
        ]
        _write_local(local, entries)
        result = delete_history_entry(None, "2026-02-01")
        assert result is True
        records = json.loads(local.read_text())
        assert len(records) == 2
        assert all(r["month"] != 2 for r in records)

    def test_returns_false_when_not_found(self, monkeypatch, tmp_path):
        local = tmp_path / "history.json"
        monkeypatch.setattr(history_mod, "_LOCAL_FILE", local)
        _write_local(local, [{"ts": "2026-01-01", "month": 1}])
        result = delete_history_entry(None, "NOT_EXIST")
        assert result is False

    def test_missing_file_returns_false(self, monkeypatch, tmp_path):
        local = tmp_path / "history.json"
        monkeypatch.setattr(history_mod, "_LOCAL_FILE", local)
        result = delete_history_entry(None, "x")
        assert result is False


# ── Table Storage success paths ───────────────────────────────────────────────

_ENTRY = dict(year=2026, month=4, month_name="Kwiecień", status="success", dry_run=False)


class TestTableStorageSuccessPaths:
    def test_append_uses_table_when_connection_available(self, monkeypatch):
        entry = build_history_entry(**_ENTRY)
        monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", "UseDevelopmentStorage=true")

        with patch("zdrovena.month_closing.table_history.append_history_table") as mock_tbl:
            append_close_history(None, entry)

        mock_tbl.assert_called_once()

    def test_append_raises_on_table_error(self, monkeypatch):
        """Table Storage errors are surfaced — no silent fallback."""
        entry = build_history_entry(**_ENTRY)
        monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", "UseDevelopmentStorage=true")

        with patch(
            "zdrovena.month_closing.table_history.append_history_table",
            side_effect=RuntimeError("table unavailable"),
        ):
            with pytest.raises(RuntimeError):
                append_close_history(None, entry)

    def test_read_uses_table_when_connection_available(self, monkeypatch):
        fake_rows = [{"ts": "2026-04-01T00:00:00Z", "month": 4, "year": 2026}]
        monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", "UseDevelopmentStorage=true")

        with patch(
            "zdrovena.month_closing.table_history.read_history_table", return_value=fake_rows
        ) as mock_tbl:
            result = read_close_history(None, limit=10)

        mock_tbl.assert_called_once()
        assert result == fake_rows

    def test_delete_uses_table_when_connection_available(self, monkeypatch):
        monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", "UseDevelopmentStorage=true")

        with patch(
            "zdrovena.month_closing.table_history.delete_history_entry_table", return_value=True
        ) as mock_tbl:
            result = delete_history_entry(None, "2026-04-01T00:00:00Z")

        mock_tbl.assert_called_once_with("UseDevelopmentStorage=true", "2026-04-01T00:00:00Z")
        assert result is True
