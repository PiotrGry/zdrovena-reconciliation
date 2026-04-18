"""Tests for zdrovena.month_closing.orchestrator — warnings gate & flags."""

from __future__ import annotations

import os
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
