"""Tests for zdrovena.month_closing.commands.preflight_cmd."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def _mock_result(missing=None, bank_found=True):
    result = MagicMock()
    result.missing_vendors = missing or []
    result.missing_reports = []
    result.bank_statement_found = bank_found
    result.warnings = []
    return result


def _make_args(period=None, period_flag=None, verbose=False, no_browser=False):
    import argparse
    return argparse.Namespace(
        period=period,
        period_flag=period_flag,
        verbose=verbose,
        no_browser=no_browser,
    )


class TestPreflightCheckerContract:
    """Verify preflight_cmd passes correct types to PreflightChecker."""

    @patch("zdrovena.month_closing.commands.preflight_cmd._get_secret", return_value=None)
    @patch("zdrovena.month_closing.preflight.PreflightChecker")
    def test_date_args_are_strings(self, mock_checker_cls, mock_secret):
        """PreflightChecker must receive date_from/date_to as 'YYYY-MM-DD' strings."""
        mock_checker_cls.return_value.run.return_value = _mock_result()

        from zdrovena.month_closing.commands.preflight_cmd import _run
        _run(_make_args(period="2025-03"))

        call_kwargs = mock_checker_cls.call_args.kwargs
        assert isinstance(call_kwargs["date_from"], str), \
            f"date_from must be str, got {type(call_kwargs['date_from'])}"
        assert isinstance(call_kwargs["date_to"], str), \
            f"date_to must be str, got {type(call_kwargs['date_to'])}"
        assert isinstance(call_kwargs["cost_date_to"], str), \
            f"cost_date_to must be str, got {type(call_kwargs['cost_date_to'])}"
        assert call_kwargs["date_from"] == "2025-03-01"
        assert call_kwargs["date_to"] == "2025-03-31"
        assert call_kwargs["cost_date_to"] == "2025-04-01"

    @patch("zdrovena.month_closing.commands.preflight_cmd._get_secret", return_value=None)
    @patch("zdrovena.month_closing.preflight.PreflightChecker")
    def test_december_rolls_over_year(self, mock_checker_cls, mock_secret):
        """December date_to should be January of next year."""
        mock_checker_cls.return_value.run.return_value = _mock_result()

        from zdrovena.month_closing.commands.preflight_cmd import _run
        _run(_make_args(period="2025-12"))

        call_kwargs = mock_checker_cls.call_args.kwargs
        assert call_kwargs["date_from"] == "2025-12-01"
        assert call_kwargs["date_to"] == "2025-12-31"
        assert call_kwargs["cost_date_to"] == "2026-01-01"

    @patch("zdrovena.month_closing.commands.preflight_cmd._get_secret", return_value=None)
    @patch("zdrovena.month_closing.preflight.PreflightChecker")
    def test_missing_vendors_exits_nonzero(self, mock_checker_cls, mock_secret):
        """Missing vendors should cause exit code 1."""
        vendor = MagicMock()
        vendor.name = "Canva"
        mock_checker_cls.return_value.run.return_value = _mock_result(missing=[vendor])

        from zdrovena.month_closing.commands.preflight_cmd import _run
        with pytest.raises(SystemExit) as exc_info:
            _run(_make_args(period="2025-03"))
        assert exc_info.value.code == 1

    @patch("zdrovena.month_closing.commands.preflight_cmd._get_secret", return_value=None)
    @patch("zdrovena.month_closing.preflight.PreflightChecker")
    def test_missing_reports_exits_nonzero(self, mock_checker_cls, mock_secret):
        """Missing reports should cause exit code 1."""
        result = _mock_result()
        result.missing_reports = [{"name": "JPK_V7M"}]
        mock_checker_cls.return_value.run.return_value = result

        from zdrovena.month_closing.commands.preflight_cmd import _run
        with pytest.raises(SystemExit) as exc_info:
            _run(_make_args(period="2025-03"))
        assert exc_info.value.code == 1

    @patch("zdrovena.month_closing.commands.preflight_cmd._get_secret", return_value=None)
    @patch("zdrovena.month_closing.preflight.PreflightChecker")
    def test_no_browser_flag_is_forwarded(self, mock_checker_cls, mock_secret):
        mock_checker_cls.return_value.run.return_value = _mock_result()

        from zdrovena.month_closing.commands.preflight_cmd import _run

        _run(_make_args(period="2025-03", no_browser=True))

        call_kwargs = mock_checker_cls.call_args.kwargs
        assert call_kwargs["no_browser"] is True


class TestPreflightPeriodParsing:
    """Verify period argument handling."""

    def test_conflict_detection(self):
        """Different positional and --period values should exit with error."""
        from zdrovena.month_closing.commands.preflight_cmd import _run
        with pytest.raises(SystemExit) as exc_info:
            _run(_make_args(period="2025-03", period_flag="2025-04"))
        assert exc_info.value.code == 1

    def test_no_period_exits(self):
        """Missing period should exit with error."""
        from zdrovena.month_closing.commands.preflight_cmd import _run
        with pytest.raises(SystemExit) as exc_info:
            _run(_make_args())
        assert exc_info.value.code == 1
