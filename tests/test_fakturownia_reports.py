"""Tests for zdrovena.month_closing.fakturownia_reports."""

from __future__ import annotations

import pytest
from unittest.mock import patch

from zdrovena.month_closing.fakturownia_reports import _get_credentials


class TestGetCredentials:
    def test_env_vars_used(self, monkeypatch):
        monkeypatch.setenv("FAKTUROWNIA_LOGIN", "user@test.com")
        monkeypatch.setenv("FAKTUROWNIA_PASSWORD", "secret")
        login, pw = _get_credentials()
        assert login == "user@test.com"
        assert pw == "secret"

    def test_missing_credentials_raises(self, monkeypatch):
        monkeypatch.delenv("FAKTUROWNIA_LOGIN", raising=False)
        monkeypatch.delenv("FAKTUROWNIA_PASSWORD", raising=False)
        with patch("keyring.get_password", return_value=None):
            with pytest.raises(RuntimeError, match="credentials not found"):
                _get_credentials()


class TestDownloadFakturowniaReports:
    def test_returns_empty_when_playwright_missing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FAKTUROWNIA_LOGIN", "x")
        monkeypatch.setenv("FAKTUROWNIA_PASSWORD", "x")
        # Patch the import inside the function to simulate missing playwright
        with patch(
            "zdrovena.month_closing.fakturownia_reports.download_fakturownia_reports"
        ) as mock_fn:
            mock_fn.return_value = []
            from zdrovena.month_closing.fakturownia_reports import download_fakturownia_reports
            result = mock_fn(
                [{"name": "JPK_FA", "url": "http://x", "dest_name": "JPK_FA.xml"}],
                "2026-03-01", "2026-04-01", tmp_path,
            )
            assert result == []


class TestPreflightNoBrowserFlag:
    def test_no_browser_skips_auto_download(self, tmp_path):
        """PreflightChecker with no_browser=True should not attempt Playwright."""
        from zdrovena.month_closing.preflight import PreflightChecker

        inbox = tmp_path / "inbox"
        inbox.mkdir()
        month_dir = tmp_path / "month"
        month_dir.mkdir()

        checker = PreflightChecker(
            year=2026, month=3,
            month_dir=month_dir,
            date_from="2026-03-01",
            date_to="2026-04-01",
            cost_date_to="2026-04-01",
            dry_run=True,
            get_secret=lambda s, required=True: None,
            no_browser=True,
        )
        with patch("zdrovena.month_closing.preflight.DOWNLOAD_WATCH_DIR", inbox):
            result = checker.run()
        # All 3 reports should be missing (no auto-download attempted)
        assert len(result.missing_reports) == 3

    def test_no_browser_default_false(self):
        """no_browser defaults to False."""
        from zdrovena.month_closing.preflight import PreflightChecker

        checker = PreflightChecker(
            year=2026, month=3,
            month_dir="/tmp/x",
            date_from="2026-03-01",
            date_to="2026-04-01",
            cost_date_to="2026-04-01",
            dry_run=True,
            get_secret=lambda s, required=True: None,
        )
        assert checker.no_browser is False
