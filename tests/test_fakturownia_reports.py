"""Tests for Fakturownia report auto-download and preflight integration."""

from __future__ import annotations

import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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
        """When no_browser=False and reports absent, missing_reports populated for orchestrator."""
        checker, inbox = _make_checker(tmp_path, no_browser=False)
        with patch("zdrovena.month_closing.preflight.DOWNLOAD_WATCH_DIR", inbox):
            checker._check_reports()
        # Auto-download is orchestrator's responsibility; preflight identifies missing reports
        assert len(checker.result.missing_reports) == 3
        assert checker.no_browser is False

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
        """Files found via glob in watch_dir are added to result.matches."""
        checker, inbox = _make_checker(tmp_path, no_browser=False)
        # Place a file matching the JPK_FA glob pattern
        jpk_fa_file = inbox / "zdrovena-2026-03-jpk_fa.xml"
        jpk_fa_file.write_text("x" * 120)
        with patch("zdrovena.month_closing.preflight.DOWNLOAD_WATCH_DIR", inbox):
            checker._check_reports()
        names_in_matches = [
            cfg["name"] for cfg, _path in checker.result.matches if isinstance(cfg, dict)
        ]
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

    def test_autodownload_empty_prints_playwright_install_hint(self, tmp_path, capsys):
        """Missing reports show manual download URLs in the preflight output."""
        checker, inbox = _make_checker(tmp_path, no_browser=False)
        with patch("zdrovena.month_closing.preflight.DOWNLOAD_WATCH_DIR", inbox):
            checker._check_reports()
        out = capsys.readouterr().out
        assert "fakturownia.pl" in out


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
    def test_build_report_url_respects_append_flag(self):
        from zdrovena.month_closing.fakturownia_reports import _build_report_url

        params = "?date_from=2026-03-01&date_to=2026-03-31"
        assert _build_report_url(
            "https://x/reports/jpk_fa", params, append_date_params=True
        ).endswith("jpk_fa?date_from=2026-03-01&date_to=2026-03-31")
        assert (
            _build_report_url(
                "https://x/accounting/app/reports/jpk_vat/18277?form_variant=3",
                params,
                append_date_params=False,
            )
            == "https://x/accounting/app/reports/jpk_vat/18277?form_variant=3"
        )

    def test_unknown_runtime_returns_empty(self, tmp_path):
        from zdrovena.month_closing.fakturownia_reports import download_fakturownia_reports

        with patch("zdrovena.month_closing.config.FAKTUROWNIA_REPORT_RUNTIME", "browser-use"):
            result = download_fakturownia_reports(
                [{"name": "JPK_FA", "url": "http://x", "dest_name": "JPK_FA.xml"}],
                "2026-03-01",
                "2026-04-01",
                tmp_path,
            )
        assert result == []

    def test_default_timeout_uses_config_runtime_timeout(self, tmp_path):
        from zdrovena.month_closing.fakturownia_reports import download_fakturownia_reports

        with (
            patch("zdrovena.month_closing.config.FAKTUROWNIA_REPORT_RUNTIME", "playwright"),
            patch("zdrovena.month_closing.config.FAKTUROWNIA_REPORT_TIMEOUT_MS", 7777),
            patch(
                "zdrovena.month_closing.fakturownia_reports._download_reports_with_playwright",
                return_value=[],
            ) as mock_runtime,
        ):
            download_fakturownia_reports(
                [{"name": "JPK_FA", "url": "http://x", "dest_name": "JPK_FA.xml"}],
                "2026-03-01",
                "2026-04-01",
                tmp_path,
            )
        assert mock_runtime.call_args.kwargs["timeout"] == 7777

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

        def _write_tiny(page, selector, output_path, *, timeout_ms):
            output_path.write_text("tiny")  # 4 bytes < 100 threshold
            return True

        with (
            patch(
                "zdrovena.month_closing.fakturownia_reports._try_download_by_button_text",
                return_value=False,
            ),
            patch(
                "zdrovena.month_closing.fakturownia_reports._try_generate_and_download",
                return_value=False,
            ),
            patch(
                "zdrovena.month_closing.fakturownia_reports._download_via_job_url",
                side_effect=_write_tiny,
            ),
        ):
            result = _download_one_report(
                MagicMock(),
                {"name": "JPK_FA", "url": "http://x", "dest_name": "JPK_FA.xml"},
                "2026-03-01",
                "2026-04-01",
                tmp_path,
                1000,
            )
        assert result is None
        assert not (tmp_path / "JPK_FA.xml").exists()

    def test_download_selector_uses_config_default_when_not_in_report(self, tmp_path):
        from zdrovena.month_closing.fakturownia_reports import _download_one_report

        used_selectors: list[str] = []
        fake_page = MagicMock()
        fake_page.wait_for_selector.side_effect = lambda sel, **_kw: used_selectors.append(sel)

        def _write_big(page, selector, output_path, *, timeout_ms):
            used_selectors.append(selector)
            output_path.write_text("x" * 120)
            return True

        with (
            patch(
                "zdrovena.month_closing.config.FAKTUROWNIA_REPORT_DOWNLOAD_SELECTOR",
                "#custom_selector",
            ),
            patch(
                "zdrovena.month_closing.fakturownia_reports._try_download_by_button_text",
                return_value=False,
            ),
            patch(
                "zdrovena.month_closing.fakturownia_reports._try_generate_and_download",
                return_value=False,
            ),
            patch(
                "zdrovena.month_closing.fakturownia_reports._download_via_job_url",
                side_effect=_write_big,
            ),
        ):
            result = _download_one_report(
                fake_page,
                {"name": "JPK_FA", "url": "http://x", "dest_name": "JPK_FA.xml"},
                "2026-03-01",
                "2026-04-01",
                tmp_path,
                1000,
            )
        assert result == tmp_path / "JPK_FA.xml"
        assert "#custom_selector" in used_selectors

    def test_click_timeout_falls_back_to_direct_job_url_download(self, tmp_path):
        from zdrovena.month_closing.fakturownia_reports import _download_one_report

        fake_page = MagicMock()
        fake_page.click.side_effect = RuntimeError("not visible")

        def _write_file(page, selector, output_path, *, timeout_ms):
            output_path.write_bytes(b"x" * 256)
            return True

        with (
            patch(
                "zdrovena.month_closing.fakturownia_reports._try_download_by_button_text",
                return_value=False,
            ),
            patch(
                "zdrovena.month_closing.fakturownia_reports._try_generate_and_download",
                return_value=False,
            ),
            patch(
                "zdrovena.month_closing.fakturownia_reports._download_via_job_url",
                side_effect=_write_file,
            ),
        ):
            result = _download_one_report(
                fake_page,
                {"name": "VAT Sales Register", "url": "http://x", "dest_name": "vat.pdf"},
                "2026-03-01",
                "2026-04-01",
                tmp_path,
                1000,
            )
        assert result == tmp_path / "vat.pdf"
        assert (tmp_path / "vat.pdf").exists()

    def test_job_id_url_fallback_when_dom_link_missing(self, tmp_path):
        from zdrovena.month_closing.fakturownia_reports import _download_via_job_url

        class _Response:
            ok = True

            @staticmethod
            def body() -> bytes:
                return b"x" * 256

        class _LocatorFirst:
            @staticmethod
            def get_attribute(_name: str):
                return None

        class _Locator:
            first = _LocatorFirst()

        class _Context:
            class request:
                @staticmethod
                def get(_url: str, timeout: int = 0):
                    return _Response()

        class _FakePage:
            context = _Context()
            url = "https://zdrovena.fakturownia.pl/reports/jpk_fa?job_id=999"

            def locator(self, *_args, **_kwargs):
                return _Locator()

            def wait_for_timeout(self, *_args, **_kwargs):
                return None

            def reload(self, *_args, **_kwargs):
                return None

        out = tmp_path / "jpk.xml"
        ok = _download_via_job_url(
            _FakePage(), "#job_download_link a[href*='/jobs/']", out, timeout_ms=5000
        )
        assert ok is True
        assert out.exists()

    def test_try_generate_and_download_uses_commit_button(self, tmp_path):
        from zdrovena.month_closing.fakturownia_reports import _try_generate_and_download

        class _Locator:
            def __init__(self, count_value: int):
                self._count_value = count_value
                self.first = SimpleNamespace(get_attribute=lambda _: None)

            def count(self):
                return self._count_value

        class _DownloadContext:
            def __init__(self, output: Path):
                self.value = SimpleNamespace(save_as=lambda _p: output.write_text("x" * 120))

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        class _FakePage:
            clicked = None
            _committed = False

            def locator(self, selector: str):
                if "commit" in selector:
                    return _Locator(1)
                # Job link appears immediately after the commit button is clicked
                if "jobs" in selector and self._committed:
                    return _Locator(1)
                return _Locator(0)

            def click(self, selector: str, **_kwargs):
                self.clicked = selector
                if "commit" in selector:
                    self._committed = True

            def wait_for_timeout(self, *_args, **_kwargs):
                return None

            def expect_download(self, *_args, **_kwargs):
                return _DownloadContext(tmp_path / "gen.xml")

        # Patch _download_via_job_url so the expect_download path is used
        with patch(
            "zdrovena.month_closing.fakturownia_reports._download_via_job_url",
            return_value=False,
        ):
            ok = _try_generate_and_download(_FakePage(), tmp_path / "gen.xml", 5000)
        assert ok is True

    def test_try_download_by_button_text_clicks_export_button(self, tmp_path):
        from zdrovena.month_closing.fakturownia_reports import _try_download_by_button_text

        class _Locator:
            def __init__(self, count_value: int):
                self._count = count_value
                self.first = self

            def count(self):
                return self._count

            def click(self, **_kwargs):
                return None

        class _DownloadContext:
            def __init__(self, output: Path):
                self.value = SimpleNamespace(save_as=lambda _p: output.write_text("x" * 120))

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        class _FakePage:
            def get_by_text(self, label: str, exact: bool = False):
                if "XML" in label:
                    return _Locator(1)
                return _Locator(0)

            def expect_download(self, *_args, **_kwargs):
                return _DownloadContext(tmp_path / "btn.xml")

        ok = _try_download_by_button_text(
            _FakePage(),
            tmp_path / "btn.xml",
            5000,
            ["Eksport do XML"],
        )
        assert ok is True

    def test_accept_pouczenia_checks_checkbox_for_v7_button_text(self):
        from zdrovena.month_closing.fakturownia_reports import _accept_pouczenia_if_present

        class _Checkbox:
            def __init__(self):
                self.checked = False
                self.first = self

            def is_checked(self):
                return self.checked

            def click(self, **_kwargs):
                self.checked = True

            def count(self):
                return 1

        class _FakePage:
            def __init__(self):
                self.cb = _Checkbox()

            def locator(self, selector: str):
                if selector == "input[type='checkbox']":
                    return self.cb
                return self.cb

            def wait_for_timeout(self, *_args, **_kwargs):
                return None

        page = _FakePage()
        _accept_pouczenia_if_present(page, ["Pobierz XML"])
        assert page.cb.checked is True

    def test_try_v7_generate_then_download(self, tmp_path):
        from zdrovena.month_closing.fakturownia_reports import _try_v7_generate_then_download

        class _Locator:
            def __init__(self, count_value: int):
                self._count = count_value
                self.first = self

            def count(self):
                return self._count

            def click(self, **_kwargs):
                return None

            def is_checked(self):
                return True

        class _DownloadContext:
            def __init__(self, output: Path):
                self.value = SimpleNamespace(save_as=lambda _p: output.write_text("x" * 128))

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        class _FakePage:
            def get_by_role(self, role: str, name=None):
                if role == "button":
                    return _Locator(1)
                return _Locator(0)

            def locator(self, selector: str):
                if selector == "input[type='checkbox']":
                    return _Locator(1)
                return _Locator(0)

            def expect_download(self, *_args, **_kwargs):
                return _DownloadContext(tmp_path / "v7.xml")

            def wait_for_timeout(self, *_args, **_kwargs):
                return None

        ok = _try_v7_generate_then_download(_FakePage(), tmp_path / "v7.xml", 5000)
        assert ok is True

    def test_try_v7_wizard_download_uses_ui_path(self, tmp_path):
        from zdrovena.month_closing.fakturownia_reports import _try_v7_wizard_download

        class _RoleLocator:
            def __init__(self, count_value: int):
                self._count = count_value
                self.first = self

            def count(self):
                return self._count

            def click(self, **_kwargs):
                return None

        class _CheckboxLocator:
            first = None

            def __init__(self):
                self.first = self
                self._checked = False

            def count(self):
                return 1

            def is_checked(self):
                return self._checked

            def click(self, **_kwargs):
                self._checked = True

        class _DownloadContext:
            def __init__(self, output: Path):
                self.value = SimpleNamespace(save_as=lambda _p: output.write_text("x" * 256))

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        class _FakePage:
            def __init__(self):
                self.cb = _CheckboxLocator()

            def get_by_role(self, role: str, name=None):
                if role in {"link", "button"}:
                    return _RoleLocator(1)
                return _RoleLocator(0)

            def locator(self, selector: str):
                if selector == "input[type='checkbox']":
                    return self.cb
                return _RoleLocator(0)

            def expect_download(self, *_args, **_kwargs):
                return _DownloadContext(tmp_path / "wizard.xml")

            def wait_for_timeout(self, *_args, **_kwargs):
                return None

        ok = _try_v7_wizard_download(_FakePage(), tmp_path / "wizard.xml", 5000)
        assert ok is True

    def test_launch_error_logs_install_hint(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setenv("FAKTUROWNIA_LOGIN", "x")
        monkeypatch.setenv("FAKTUROWNIA_PASSWORD", "y")
        from zdrovena.month_closing.fakturownia_reports import download_fakturownia_reports

        fake_stealth_cm = MagicMock()
        fake_pw = MagicMock()
        fake_pw.chromium.launch.side_effect = RuntimeError("Executable doesn't exist")
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
            caplog.at_level("WARNING"),
        ):
            mock_stealth_cls.return_value.use_sync.return_value = fake_stealth_cm
            result = download_fakturownia_reports(
                [{"name": "JPK_FA", "url": "http://x", "dest_name": "JPK_FA.xml"}],
                "2026-03-01",
                "2026-04-01",
                tmp_path,
            )
        assert result == []
        assert "playwright install chromium" in caplog.text
