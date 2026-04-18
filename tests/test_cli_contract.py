"""CLI contract tests — snapshot CloseReport structure.

These tests lock down the public shape of CloseReport so that
refactors (e.g. moving to API client in Faza D) can't silently
drop or rename fields without a failing test.

Strategy: run MonthCloseOrchestrator.execute() in dry_run=True with
all external I/O mocked out. Assert on field names and types, not values.
"""

from __future__ import annotations

from dataclasses import fields
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from zdrovena.month_closing.orchestrator import CloseReport, MonthCloseOrchestrator


# ── CloseReport field contract ────────────────────────────────────────────────

class TestCloseReportContract:
    """Lock down the public fields of CloseReport."""

    EXPECTED_FIELDS = {
        "sales_invoice_count": int,
        "sales_gross_total": Decimal,
        "sales_pdfs_downloaded": int,
        "cost_invoice_count": int,
        "cost_found_vendors": dict,
        "cost_missing_vendors": list,
        "ksef_count": int,
        "bank_statement_found": bool,
        "zip_path": (Path, type(None)),
        "email_sent": bool,
        "warnings": list,
        "errors": list,
        "steps_completed": list,
    }

    def test_all_expected_fields_present(self):
        actual = {f.name for f in fields(CloseReport)}
        missing = set(self.EXPECTED_FIELDS) - actual
        assert not missing, f"CloseReport missing fields: {missing}"

    def test_no_unexpected_fields_added(self):
        """Catch silent additions that may need API schema changes."""
        actual = {f.name for f in fields(CloseReport)}
        unexpected = actual - set(self.EXPECTED_FIELDS)
        assert not unexpected, (
            f"New CloseReport fields detected (update contract if intentional): {unexpected}"
        )

    def test_default_values(self):
        r = CloseReport()
        assert r.sales_invoice_count == 0
        assert r.sales_gross_total == Decimal("0.00")
        assert r.cost_found_vendors == {}
        assert r.cost_missing_vendors == []
        assert r.warnings == []
        assert r.errors == []
        assert r.steps_completed == []
        assert r.zip_path is None
        assert r.email_sent is False
        assert r.bank_statement_found is False

    def test_has_critical_errors_property(self):
        r = CloseReport()
        assert r.has_critical_errors is False
        r.errors.append("boom")
        assert r.has_critical_errors is True


# ── execute() dry_run contract ────────────────────────────────────────────────

_STEP_PATCHES = [
    "zdrovena.month_closing.orchestrator.MonthCloseOrchestrator._step_0_preflight",
    "zdrovena.month_closing.orchestrator.MonthCloseOrchestrator._step_1_create_folders",
    "zdrovena.month_closing.orchestrator.MonthCloseOrchestrator._step_2_sales_invoices",
    "zdrovena.month_closing.orchestrator.MonthCloseOrchestrator._step_3_jpk_reports",
    "zdrovena.month_closing.orchestrator.MonthCloseOrchestrator._step_4_cost_invoices",
    "zdrovena.month_closing.orchestrator.MonthCloseOrchestrator._step_5_bank_statement",
    "zdrovena.month_closing.orchestrator.MonthCloseOrchestrator._step_6_zip_archive",
    "zdrovena.month_closing.orchestrator.MonthCloseOrchestrator._step_7_email",
    "zdrovena.month_closing.orchestrator.MonthCloseOrchestrator._check_warnings_gate",
]


def _run_dry(year: int = 2025, month: int = 6) -> CloseReport:
    """Run execute() with all pipeline steps mocked — returns CloseReport."""
    with patch.multiple("zdrovena.month_closing.orchestrator.MonthCloseOrchestrator",
                        _step_0_preflight=MagicMock(),
                        _step_1_create_folders=MagicMock(),
                        _step_2_sales_invoices=MagicMock(),
                        _step_3_jpk_reports=MagicMock(),
                        _step_4_cost_invoices=MagicMock(),
                        _step_5_bank_statement=MagicMock(),
                        _step_6_zip_archive=MagicMock(),
                        _step_7_email=MagicMock(),
                        _check_warnings_gate=MagicMock()):
        orch = MonthCloseOrchestrator(year=year, month=month, dry_run=True)
        orch.out = MagicMock()
        return orch.execute()


class TestExecuteDryRunContract:
    """execute() must return a CloseReport regardless of pipeline outcome."""

    def test_returns_close_report(self):
        result = _run_dry()
        assert isinstance(result, CloseReport)

    def test_report_is_serialisable_to_dict(self):
        """Fields can be iterated — prerequisite for JSON API response in Faza C."""
        from dataclasses import asdict
        result = _run_dry()
        d = asdict(result)
        assert "sales_invoice_count" in d
        assert "steps_completed" in d
        assert "errors" in d

    def test_dry_run_flag_propagates(self):
        with patch.multiple("zdrovena.month_closing.orchestrator.MonthCloseOrchestrator",
                            _step_0_preflight=MagicMock(),
                            _step_1_create_folders=MagicMock(),
                            _step_2_sales_invoices=MagicMock(),
                            _step_3_jpk_reports=MagicMock(),
                            _step_4_cost_invoices=MagicMock(),
                            _step_5_bank_statement=MagicMock(),
                            _step_6_zip_archive=MagicMock(),
                            _step_7_email=MagicMock(),
                            _check_warnings_gate=MagicMock()):
            orch = MonthCloseOrchestrator(year=2025, month=6, dry_run=True)
            orch.out = MagicMock()
            orch.execute()
            assert orch.dry_run is True

    def test_invalid_month_raises(self):
        with pytest.raises(ValueError, match="month"):
            MonthCloseOrchestrator(year=2025, month=13)

    def test_invalid_year_raises(self):
        with pytest.raises(ValueError, match="year"):
            MonthCloseOrchestrator(year=2019, month=6)
