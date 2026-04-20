"""Tests for zdrovena.month_closing.orchestrator — warnings gate & flags."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from zdrovena.month_closing.orchestrator import CloseReport, MonthCloseOrchestrator

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_orchestrator(
    *,
    ignore_warnings: bool = False,
    non_interactive: bool = False,
) -> MonthCloseOrchestrator:
    """Create an orchestrator with sensible defaults for testing."""
    orch = MonthCloseOrchestrator(
        year=2025,
        month=6,
        dry_run=True,
        non_interactive=non_interactive,
        ignore_warnings=ignore_warnings,
    )
    # Mute console output during tests
    orch.out = MagicMock()
    return orch


# ── Warnings Gate ─────────────────────────────────────────────────────────────


class TestWarningsGate:
    def test_strict_mode_raises(self):
        """Default (strict): warnings gate should abort the pipeline."""
        orch = _make_orchestrator(ignore_warnings=False)
        orch.report.warnings.append("Missing JPK_V7M")

        with pytest.raises(RuntimeError, match="warning"):
            orch._check_warnings_gate()

    def test_ignore_warnings_continues(self):
        """With --ignore-warnings, gate should NOT raise."""
        orch = _make_orchestrator(ignore_warnings=True)
        orch.report.warnings.append("Missing JPK_V7M")

        # Should not raise
        orch._check_warnings_gate()

    def test_no_warnings_passes(self):
        """No warnings → gate passes regardless of flag."""
        for flag in (True, False):
            orch = _make_orchestrator(ignore_warnings=flag)
            orch._check_warnings_gate()  # should not raise

    def test_email_blocked_despite_ignore_warnings(self):
        """Even with --ignore-warnings, email should be blocked if warnings exist."""
        orch = _make_orchestrator(ignore_warnings=True)
        orch.report.warnings.append("Minor issue")

        # _step_7_email checks report.warnings directly
        with pytest.raises(RuntimeError, match="Cannot send email"):
            orch._step_7_email()


# ── Non-interactive mode ──────────────────────────────────────────────────────


class TestNonInteractive:
    def test_flag_stored(self):
        orch = _make_orchestrator(non_interactive=True)
        assert orch.non_interactive is True

    def test_flag_default_false(self):
        orch = _make_orchestrator()
        assert orch.non_interactive is False

    @patch("zdrovena.month_closing.orchestrator.PreflightChecker")
    def test_step0_preflight_forwards_non_interactive_as_no_browser(self, mock_checker_cls):
        orch = _make_orchestrator(non_interactive=True)
        mock_checker = MagicMock()
        mock_checker.run.return_value = SimpleNamespace(
            bank_statement_found=True,
            warnings=[],
        )
        mock_checker.build_blockers.return_value = []
        mock_checker_cls.return_value = mock_checker

        orch._step_0_preflight()

        call_kwargs = mock_checker_cls.call_args.kwargs
        assert call_kwargs["no_browser"] is True


# ── Constructor validation ────────────────────────────────────────────────────


class TestOrchestratorInit:
    def test_invalid_month(self):
        with pytest.raises(ValueError, match="Invalid month"):
            MonthCloseOrchestrator(year=2025, month=13)

    def test_suspicious_year(self):
        with pytest.raises(ValueError, match="Suspicious year"):
            MonthCloseOrchestrator(year=2010, month=1)

    def test_valid_construction(self):
        orch = MonthCloseOrchestrator(year=2025, month=6, dry_run=True)
        assert orch.year == 2025
        assert orch.month == 6
        assert orch.date_from == "2025-06-01"
        assert orch.date_to == "2025-06-30"


# ── _get_secret env fallback ─────────────────────────────────────────────────


class TestGetSecretEnvFallback:
    @patch.dict(os.environ, {"ZOHO_SMTP_PASSWORD": "env_pass"})
    @patch("zdrovena.common.secrets.keyring.get_password", return_value=None)
    def test_env_var_used(self, mock_kr):
        result = MonthCloseOrchestrator._get_secret("zoho_smtp_password")
        assert result == "env_pass"

    @patch.dict(os.environ, {}, clear=True)
    @patch("zdrovena.common.secrets.keyring.get_password", return_value="kr_val")
    def test_keyring_fallback(self, mock_kr):
        os.environ.pop("ZOHO_SMTP_PASSWORD", None)
        result = MonthCloseOrchestrator._get_secret("zoho_smtp_password")
        assert result == "kr_val"

    @patch.dict(os.environ, {}, clear=True)
    @patch("zdrovena.common.secrets.keyring.get_password", return_value=None)
    def test_neither_raises(self, mock_kr):
        os.environ.pop("ZOHO_SMTP_PASSWORD", None)
        from zdrovena.common.exceptions import MissingSecretError

        with pytest.raises(MissingSecretError):
            MonthCloseOrchestrator._get_secret("zoho_smtp_password")

    @patch.dict(os.environ, {}, clear=True)
    @patch("zdrovena.common.secrets.keyring.get_password", return_value=None)
    def test_not_required_returns_none(self, mock_kr):
        os.environ.pop("SOME_SERVICE", None)
        result = MonthCloseOrchestrator._get_secret("some_service", required=False)
        assert result is None


# ── CloseReport ───────────────────────────────────────────────────────────────


class TestCloseReport:
    def test_defaults(self):
        r = CloseReport()
        assert r.sales_invoice_count == 0
        assert r.has_critical_errors is False

    def test_has_critical_errors(self):
        r = CloseReport()
        r.errors.append("fatal")
        assert r.has_critical_errors is True


# ── _mark_step_done & _skip_if_done ─────────────────────────────────────────


class TestStepTracking:
    def test_mark_step_done_dry_run_does_not_persist(self):
        """dry_run=True should not call state.mark_done."""
        orch = _make_orchestrator()
        orch.state = MagicMock()
        orch._mark_step_done("My Step")
        orch.state.mark_done.assert_not_called()
        assert "My Step" in orch.report.steps_completed

    def test_skip_if_done_returns_true_when_done(self):
        orch = _make_orchestrator()
        orch.state = MagicMock()
        orch.state.is_done.return_value = True
        assert orch._skip_if_done("Foo") is True
        assert "Foo" in orch.report.steps_completed

    def test_skip_if_done_returns_false_when_not_done(self):
        orch = _make_orchestrator()
        orch.state = MagicMock()
        orch.state.is_done.return_value = False
        assert orch._skip_if_done("Foo") is False


# ── _build_email_body ────────────────────────────────────────────────────────


class TestBuildEmailBody:
    def test_contains_month_name(self):
        orch = _make_orchestrator()
        body = orch._build_email_body()
        assert orch.month_pl in body

    def test_contains_greeting(self):
        orch = _make_orchestrator()
        body = orch._build_email_body()
        assert "Dzień dobry" in body

    def test_is_string(self):
        orch = _make_orchestrator()
        assert isinstance(orch._build_email_body(), str)


# ── _print_summary ───────────────────────────────────────────────────────────


class TestPrintSummary:
    def test_summary_called_without_error(self):
        orch = _make_orchestrator()
        orch._print_summary()  # should not raise
        orch.out.summary_header.assert_called_once()

    def test_summary_with_warnings_and_errors(self):
        orch = _make_orchestrator()
        orch.report.warnings.append("A warning")
        orch.report.errors.append("An error")
        orch._print_summary()
        # summary_footer called with success=False
        orch.out.summary_footer.assert_called_with(success=False)

    def test_summary_with_zip_path(self):
        orch = _make_orchestrator()
        orch.report.zip_path = Path("/tmp/june_2025_HUMIO.zip")
        orch.report.email_sent = True
        orch.report.cost_found_vendors = {"Vendor A": "Fakturownia"}
        orch._print_summary()
        orch.out.summary_footer.assert_called_with(success=True)


# ── _step_5_bank_statement ───────────────────────────────────────────────────


class TestStep5BankStatement:
    def test_pko_file_found(self, tmp_path):
        orch = _make_orchestrator()
        orch.month_dir = tmp_path
        # Create a matching PDF
        (tmp_path / "wyciag_czerwiec.pdf").write_bytes(b"%PDF")
        orch._step_5_bank_statement()
        assert orch.report.bank_statement_found is True

    def test_pko_file_found_via_name(self, tmp_path):
        orch = _make_orchestrator()
        orch.month_dir = tmp_path
        (tmp_path / "pko_2025-06.pdf").write_bytes(b"%PDF")
        orch._step_5_bank_statement()
        assert orch.report.bank_statement_found is True

    def test_no_file_but_preflight_found(self, tmp_path):
        orch = _make_orchestrator()
        orch.month_dir = tmp_path
        orch.report.bank_statement_found = True
        orch._step_5_bank_statement()  # should not raise

    def test_no_file_raises(self, tmp_path):
        orch = _make_orchestrator()
        orch.month_dir = tmp_path
        orch.report.bank_statement_found = False
        with pytest.raises(RuntimeError, match="Bank statement"):
            orch._step_5_bank_statement()


# ── _step_6_zip_archive ──────────────────────────────────────────────────────


class TestStep6ZipArchive:
    def test_dry_run_does_not_create_zip(self):
        orch = _make_orchestrator()  # dry_run=True
        orch._step_6_zip_archive()
        assert orch.report.zip_path is None
        assert any("ZIP archive" in s for s in orch.report.steps_completed)

    @patch("zdrovena.month_closing.orchestrator.create_month_archive")
    def test_live_creates_zip(self, mock_archive, tmp_path):
        orch = MonthCloseOrchestrator(year=2025, month=6, dry_run=False)
        orch.out = MagicMock()
        orch.month_dir = tmp_path
        fake_zip = tmp_path / "czerwiec_2025_HUMIO.zip"
        mock_archive.return_value = fake_zip
        orch._step_6_zip_archive()
        assert orch.report.zip_path == fake_zip
        mock_archive.assert_called_once()


# ── _step_7_email ────────────────────────────────────────────────────────────


class TestStep7Email:
    def test_dry_run_does_not_send(self):
        orch = _make_orchestrator()  # dry_run=True
        orch._step_7_email()
        assert orch.report.email_sent is False
        assert any("Email" in s for s in orch.report.steps_completed)

    def test_errors_block_email(self):
        orch = _make_orchestrator()
        orch.report.errors.append("Pipeline error")
        with pytest.raises(RuntimeError, match="Cannot send email"):
            orch._step_7_email()

    @patch("zdrovena.month_closing.orchestrator.EmailService")
    @patch("zdrovena.month_closing.orchestrator.MonthCloseOrchestrator._get_secret")
    def test_live_sends_email(self, mock_secret, mock_email_cls, tmp_path):
        orch = MonthCloseOrchestrator(year=2025, month=6, dry_run=False)
        orch.out = MagicMock()
        mock_secret.return_value = "smtp_pass"
        mock_svc = MagicMock()
        mock_email_cls.return_value = mock_svc
        orch._step_7_email()
        mock_svc.send_report.assert_called_once()
        assert orch.report.email_sent is True


# ── _step_1_create_folders ───────────────────────────────────────────────────


class TestStep1CreateFolders:
    def test_creates_directories(self, tmp_path):
        orch = _make_orchestrator()
        orch.month_dir = tmp_path / "2025" / "czerwiec"
        orch.sales_dir = orch.month_dir / "sprzedaz"
        orch.costs_dir = orch.month_dir / "koszty"
        orch._preflight_checker = None
        orch._step_1_create_folders()
        assert orch.sales_dir.exists()
        assert orch.costs_dir.exists()

    def test_copies_preflight_files(self, tmp_path):
        orch = _make_orchestrator()
        orch.month_dir = tmp_path / "2025" / "czerwiec"
        orch.sales_dir = orch.month_dir / "sprzedaz"
        orch.costs_dir = orch.month_dir / "koszty"
        mock_checker = MagicMock()
        orch._preflight_checker = mock_checker
        orch._step_1_create_folders()
        mock_checker.copy_to_folders.assert_called_once_with(orch.month_dir, orch.costs_dir)


# ── _step_0_preflight — blockers path ───────────────────────────────────────


class TestStep0PreflightBlockers:
    @patch("zdrovena.month_closing.orchestrator.PreflightChecker")
    def test_blockers_raise_system_exit(self, mock_checker_cls):
        orch = _make_orchestrator()
        mock_checker = MagicMock()
        mock_checker.run.return_value = SimpleNamespace(
            bank_statement_found=False,
            warnings=[],
        )
        mock_checker.build_blockers.return_value = ["Missing: invoice.pdf"]
        mock_checker_cls.return_value = mock_checker

        with pytest.raises(SystemExit):
            orch._step_0_preflight()

    @patch("zdrovena.month_closing.orchestrator.PreflightChecker")
    def test_warnings_forwarded_to_report(self, mock_checker_cls):
        orch = _make_orchestrator()
        mock_checker = MagicMock()
        mock_checker.run.return_value = SimpleNamespace(
            bank_statement_found=True,
            warnings=["Date mismatch in invoice"],
        )
        mock_checker.build_blockers.return_value = []
        mock_checker_cls.return_value = mock_checker

        orch._step_0_preflight()
        assert "Date mismatch in invoice" in orch.report.warnings


# ── _step_2_sales_invoices ───────────────────────────────────────────────────


class TestStep2SalesInvoices:
    @patch("zdrovena.month_closing.orchestrator.FakturowniaClient")
    def test_no_invoices_raises(self, mock_client_cls):
        orch = _make_orchestrator()
        mock_client = MagicMock()
        mock_client.fetch_sales_invoices.return_value = []
        mock_client_cls.from_keyring.return_value = mock_client

        with pytest.raises(RuntimeError, match="No sales invoices"):
            orch._step_2_sales_invoices()

    @patch("zdrovena.month_closing.orchestrator.FakturowniaClient")
    def test_numbering_gap_raises(self, mock_client_cls):
        orch = _make_orchestrator()
        mock_client = MagicMock()
        # Invoices 1 and 3 in same series — gap at 2
        mock_client.fetch_sales_invoices.return_value = [
            {"number": "1/06/2025", "price_gross": "100.00", "positions": []},
            {"number": "3/06/2025", "price_gross": "200.00", "positions": []},
        ]
        mock_client.download_all_pdfs.return_value = []
        mock_client_cls.from_keyring.return_value = mock_client

        with pytest.raises(RuntimeError, match="Brakuje faktur"):
            orch._step_2_sales_invoices()

    @patch("zdrovena.month_closing.orchestrator.FakturowniaClient")
    def test_successful_download(self, mock_client_cls):
        orch = _make_orchestrator()
        mock_client = MagicMock()
        mock_client.fetch_sales_invoices.return_value = [
            {"number": "1/06/2025", "price_gross": "100.00", "positions": []},
            {"number": "2/06/2025", "price_gross": "200.00", "positions": []},
        ]
        mock_client.download_all_pdfs.return_value = [Path("/tmp/a.pdf"), Path("/tmp/b.pdf")]
        mock_client_cls.from_keyring.return_value = mock_client

        orch._step_2_sales_invoices()
        assert orch.report.sales_invoice_count == 2
        assert orch.report.sales_pdfs_downloaded == 2


# ── _step_3_jpk_reports ──────────────────────────────────────────────────────


class TestStep3JpkReports:
    @patch("zdrovena.month_closing.orchestrator.FAKTUROWNIA_REPORTS", [])
    def test_no_reports_required_passes(self):
        orch = _make_orchestrator()
        orch._step_3_jpk_reports()  # should not raise
        assert any("JPK" in s for s in orch.report.steps_completed)

    def test_missing_reports_dry_run_raises(self, tmp_path):
        """In non-interactive dry_run, missing reports should raise."""
        orch = _make_orchestrator(non_interactive=True)
        orch.month_dir = tmp_path
        # Don't create any report files — all will be missing
        with patch(
            "zdrovena.month_closing.orchestrator.FAKTUROWNIA_REPORTS",
            [{"name": "JPK_FA", "dest_name": "jpk_fa.pdf"}],
        ):
            with pytest.raises(RuntimeError, match="JPK"):
                orch._step_3_jpk_reports()

    def test_all_reports_present_passes(self, tmp_path):
        orch = _make_orchestrator()
        orch.month_dir = tmp_path
        (tmp_path / "jpk_fa.pdf").write_bytes(b"%PDF")
        with patch(
            "zdrovena.month_closing.orchestrator.FAKTUROWNIA_REPORTS",
            [{"name": "JPK_FA", "dest_name": "jpk_fa.pdf"}],
        ):
            orch._step_3_jpk_reports()
            assert any("JPK" in s for s in orch.report.steps_completed)


# ── execute modes ─────────────────────────────────────────────────────────────


class TestExecuteModes:
    def _mock_all_steps(self, orch: MonthCloseOrchestrator) -> None:
        """Patch all pipeline steps on the orchestrator instance."""
        for step in [
            "_step_0_preflight",
            "_step_1_create_folders",
            "_step_2_sales_invoices",
            "_step_3_jpk_reports",
            "_step_4_cost_invoices",
            "_step_5_bank_statement",
            "_check_warnings_gate",
            "_step_6_zip_archive",
            "_step_7_email",
            "_print_summary",
        ]:
            setattr(orch, step, MagicMock())

    def test_execute_calls_all_steps(self):
        orch = _make_orchestrator()
        self._mock_all_steps(orch)
        report = orch.execute()
        orch._step_0_preflight.assert_called_once()
        orch._step_7_email.assert_called_once()
        assert isinstance(report, CloseReport)

    def test_execute_zip_only_skips_email(self):
        orch = _make_orchestrator()
        self._mock_all_steps(orch)
        orch.execute_zip_only()
        orch._step_6_zip_archive.assert_called_once()
        orch._step_7_email.assert_not_called()

    def test_execute_send_only_zip_missing(self, tmp_path):
        orch = _make_orchestrator()
        orch.month_dir = tmp_path  # no ZIP file here
        orch._print_summary = MagicMock()
        report = orch.execute_send_only()
        assert report.errors  # should have an error about missing ZIP

    def test_execute_send_only_zip_found(self, tmp_path):
        orch = _make_orchestrator()
        zip_path = tmp_path / "czerwiec_2025_HUMIO.zip"
        zip_path.write_bytes(b"PK")
        orch.month_dir = tmp_path
        orch._step_7_email = MagicMock()
        orch._print_summary = MagicMock()
        orch.execute_send_only()
        assert orch.report.zip_path == zip_path
        orch._step_7_email.assert_called_once()

    def test_execute_zip_and_send(self):
        orch = _make_orchestrator()
        orch._step_6_zip_archive = MagicMock()
        orch._step_7_email = MagicMock()
        orch._print_summary = MagicMock()
        orch.execute_zip_and_send()
        orch._step_6_zip_archive.assert_called_once()
        orch._step_7_email.assert_called_once()

    def test_execute_records_error_on_exception(self):
        orch = _make_orchestrator()
        self._mock_all_steps(orch)
        orch._step_0_preflight.side_effect = RuntimeError("boom")
        with pytest.raises(RuntimeError):
            orch.execute()
        assert any("boom" in e for e in orch.report.errors)
