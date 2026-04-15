"""Tests for Fakturownia report auto-download and preflight integration."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import types

import pytest

from zdrovena.month_closing.fakturownia_reports import _get_credentials
from zdrovena.month_closing.preflight import PreflightChecker


def _make_checker(tmp_path: Path, *, no_browser: bool) -> tuple[PreflightChecker, Path]:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    month_dir = tmp_path / "month"
    month_dir.mkdir()
    checker = PreflightChecker(
        year=2026,
        month=3,
        month_dir=month_dir,
        date_from="2026-03-01",
        date_to="2026-04-01",
        cost_date_to="2026-04-01",
        dry_run=True,
        get_secret=lambda _service, required=True: None,
        no_browser=no_browser,
    )
    return checker, inbox


class TestPreflightReportBoundary:
    def test_missing_reports_calls_autodownload_when_browser_enabled(self, tmp_path):
        checker, inbox = _make_checker(tmp_path, no_browser=False)
        with (
            patch("zdrovena.month_closing.preflight.DOWNLOAD_WATCH_DIR", inbox),
            patch(
                "zdrovena.month_closing.fakturownia_reports.download_fakturownia_reports",
                return_value=[],
            ) as mock_download,
        ):
            checker._check_reports()
        assert mock_download.called

    def test_no_browser_skips_autodownload(self, tmp_path):
        checker, inbox = _make_checker(tmp_path, no_browser=True)
        with (
            patch("zdrovena.month_closing.preflight.DOWNLOAD_WATCH_DIR", inbox),
            patch(
                "zdrovena.month_closing.fakturownia_reports.download_fakturownia_reports",
            ) as mock_download,
        ):
            checker._check_reports()
        assert not mock_download.called
        assert len(checker.result.missing_reports) == 3

    def test_successful_autodownload_moves_report_to_matches(self, tmp_path):
        checker, inbox = _make_checker(tmp_path, no_browser=False)
        downloaded = inbox / "JPK_FA.xml"
        downloaded.write_text("x" * 120)
        jpk_fa = {
            "name": "JPK_FA",
            "glob": "zdrovena-*-jpk_fa*",
            "dest_name": "JPK_FA.xml",
            "url": "https://zdrovena.fakturownia.pl/reports/jpk_fa",
        }
        with (
            patch("zdrovena.month_closing.preflight.DOWNLOAD_WATCH_DIR", inbox),
            patch(
                "zdrovena.month_closing.fakturownia_reports.download_fakturownia_reports",
                return_value=[(jpk_fa, downloaded)],
            ),
        ):
            checker._check_reports()
        names_in_matches = [cfg["name"] for cfg, _path in checker.result.matches if isinstance(cfg, dict)]
        assert "JPK_FA" in names_in_matches
        missing_names = [r["name"] for r in checker.result.missing_reports]
        assert "JPK_FA" not in missing_names

    def test_autodownload_exception_falls_back_to_manual_missing(self, tmp_path):
        checker, inbox = _make_checker(tmp_path, no_browser=False)
        with (
            patch("zdrovena.month_closing.preflight.DOWNLOAD_WATCH_DIR", inbox),
            patch(
                "zdrovena.month_closing.fakturownia_reports.download_fakturownia_reports",
                side_effect=RuntimeError("boom"),
            ),
        ):
            checker._check_reports()
        assert len(checker.result.missing_reports) == 3


class TestGetCredentials:
    def test_env_vars_override_keyring(self, monkeypatch):
        monkeypatch.setenv("FAKTUROWNIA_LOGIN", "user@test.com")
        monkeypatch.setenv("FAKTUROWNIA_PASSWORD", "secret")
        with patch("keyring.get_password", return_value="from-keyring"):
            login, pw = _get_credentials()
        assert login == "user@test.com"
        assert pw == "secret"

    def test_keyring_used_when_env_missing(self, monkeypatch):
        monkeypatch.delenv("FAKTUROWNIA_LOGIN", raising=False)
        monkeypatch.delenv("FAKTUROWNIA_PASSWORD", raising=False)
        with patch("keyring.get_password", side_effect=["login-from-kr", "pass-from-kr"]):
            login, pw = _get_credentials()
        assert login == "login-from-kr"
        assert pw == "pass-from-kr"

    def test_missing_credentials_raises(self, monkeypatch):
        monkeypatch.delenv("FAKTUROWNIA_LOGIN", raising=False)
        monkeypatch.delenv("FAKTUROWNIA_PASSWORD", raising=False)
        with patch("keyring.get_password", return_value=None):
            with pytest.raises(RuntimeError, match="credentials not found"):
                _get_credentials()


class TestDownloadFakturowniaReportsContract:
    def test_returns_empty_when_playwright_missing(self, tmp_path):
        from zdrovena.month_closing.fakturownia_reports import download_fakturownia_reports

        with patch.dict(
            "sys.modules",
            {"playwright": None, "playwright.sync_api": None, "playwright_stealth": None},
        ):
            result = download_fakturownia_reports(
                [{"name": "JPK_FA", "url": "http://x", "dest_name": "JPK_FA.xml"}],
                "2026-03-01",
                "2026-04-01",
                tmp_path,
            )
        assert result == []

    def test_login_failure_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FAKTUROWNIA_LOGIN", "x")
        monkeypatch.setenv("FAKTUROWNIA_PASSWORD", "y")
        from zdrovena.month_closing.fakturownia_reports import download_fakturownia_reports

        fake_stealth_cm = MagicMock()
        fake_pw = MagicMock()
        fake_browser = MagicMock()
        fake_context = MagicMock()
        fake_page = MagicMock()
        fake_context.new_page.return_value = fake_page
        fake_browser.new_context.return_value = fake_context
        fake_pw.chromium.launch.return_value = fake_browser
        fake_stealth_cm.__enter__.return_value = fake_pw
        fake_stealth_cm.__exit__.return_value = False

        fake_playwright_sync_api = types.ModuleType("playwright.sync_api")
        fake_playwright_sync_api.sync_playwright = lambda: object()
        fake_stealth_module = types.ModuleType("playwright_stealth")
        mock_stealth_cls = MagicMock()
        fake_stealth_module.Stealth = mock_stealth_cls

        with (
            patch.dict(
                "sys.modules",
                {
                    "playwright.sync_api": fake_playwright_sync_api,
                    "playwright_stealth": fake_stealth_module,
                },
            ),
            patch(
                "zdrovena.month_closing.fakturownia_reports._login",
                side_effect=RuntimeError("login failed"),
            ),
        ):
            mock_stealth_cls.return_value.use_sync.return_value = fake_stealth_cm
            result = download_fakturownia_reports(
                [{"name": "JPK_FA", "url": "http://x", "dest_name": "JPK_FA.xml"}],
                "2026-03-01",
                "2026-04-01",
                tmp_path,
            )
        assert result == []

    def test_missing_credentials_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.delenv("FAKTUROWNIA_LOGIN", raising=False)
        monkeypatch.delenv("FAKTUROWNIA_PASSWORD", raising=False)
        from zdrovena.month_closing.fakturownia_reports import download_fakturownia_reports

        with patch(
            "zdrovena.month_closing.fakturownia_reports._get_credentials",
            side_effect=RuntimeError("credentials not found"),
        ):
            result = download_fakturownia_reports(
                [{"name": "JPK_FA", "url": "http://x", "dest_name": "JPK_FA.xml"}],
                "2026-03-01",
                "2026-04-01",
                tmp_path,
            )
        assert result == []

    def test_report_timeout_is_skipped_and_next_report_continues(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FAKTUROWNIA_LOGIN", "x")
        monkeypatch.setenv("FAKTUROWNIA_PASSWORD", "y")
        from zdrovena.month_closing.fakturownia_reports import download_fakturownia_reports

        fake_stealth_cm = MagicMock()
        fake_pw = MagicMock()
        fake_browser = MagicMock()
        fake_context = MagicMock()
        fake_page = MagicMock()
        fake_context.new_page.return_value = fake_page
        fake_browser.new_context.return_value = fake_context
        fake_pw.chromium.launch.return_value = fake_browser
        fake_stealth_cm.__enter__.return_value = fake_pw
        fake_stealth_cm.__exit__.return_value = False

        ok_path = tmp_path / "JPK_V7M.xml"
        reports = [
            {"name": "JPK_FA", "url": "http://x/a", "dest_name": "JPK_FA.xml"},
            {"name": "JPK_V7M", "url": "http://x/b", "dest_name": "JPK_V7M.xml"},
        ]

        fake_playwright_sync_api = types.ModuleType("playwright.sync_api")
        fake_playwright_sync_api.sync_playwright = lambda: object()
        fake_stealth_module = types.ModuleType("playwright_stealth")
        mock_stealth_cls = MagicMock()
        fake_stealth_module.Stealth = mock_stealth_cls

        with (
            patch.dict(
                "sys.modules",
                {
                    "playwright.sync_api": fake_playwright_sync_api,
                    "playwright_stealth": fake_stealth_module,
                },
            ),
            patch("zdrovena.month_closing.fakturownia_reports._login", return_value=None),
            patch(
                "zdrovena.month_closing.fakturownia_reports._download_one_report",
                side_effect=[None, ok_path],
            ),
        ):
            mock_stealth_cls.return_value.use_sync.return_value = fake_stealth_cm
            result = download_fakturownia_reports(
                reports,
                "2026-03-01",
                "2026-04-01",
                tmp_path,
            )
        assert result == [(reports[1], ok_path)]

    def test_small_download_file_is_removed(self, tmp_path):
        from zdrovena.month_closing.fakturownia_reports import _download_one_report

        class _DownloadContext:
            def __init__(self, target_path: Path):
                self.target_path = target_path
                self.value = SimpleNamespace(save_as=self._save_as)

            def _save_as(self, _path: str) -> None:
                self.target_path.write_text("tiny")

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        class _FakePage:
            def goto(self, *_args, **_kwargs):
                return None

            def wait_for_timeout(self, *_args, **_kwargs):
                return None

            def wait_for_selector(self, *_args, **_kwargs):
                return None

            def expect_download(self, *_args, **_kwargs):
                return _DownloadContext(tmp_path / "JPK_FA.xml")

            def click(self, *_args, **_kwargs):
                return None

        result = _download_one_report(
            _FakePage(),
            {"name": "JPK_FA", "url": "http://x", "dest_name": "JPK_FA.xml"},
            "2026-03-01",
            "2026-04-01",
            tmp_path,
            1000,
        )
        assert result is None
        assert not (tmp_path / "JPK_FA.xml").exists()
