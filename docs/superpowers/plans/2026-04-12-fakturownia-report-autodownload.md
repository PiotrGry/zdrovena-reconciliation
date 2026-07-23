# Fakturownia Report Auto-Download Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Auto-download JPK_FA.xml, JPK_V7M.xml, and Wykaz_sprzedazy_VAT.pdf from Fakturownia via Playwright during preflight, eliminating manual download.

**Architecture:** New module `fakturownia_reports.py` handles Playwright login + download. Preflight calls it when reports are missing, falls back to manual URLs on failure. Single browser session for all 3 reports.

**Tech Stack:** Python, Playwright, playwright-stealth, keyring

---

### Task 1: Create `fakturownia_reports.py` with download logic

**Files:**
- Create: `zdrovena/month_closing/fakturownia_reports.py`

- [ ] **Step 1: Create the module with imports and logger**

```python
"""
zdrovena.month_closing.fakturownia_reports – Auto-download Fakturownia reports
===============================================================================
Downloads JPK_FA, JPK_V7M, and VAT Sales Register from Fakturownia's web UI
using Playwright. These reports are only available via the browser interface.

Flow:
1. Login at /login (form POST)
2. For each report: navigate with submitted=true, wait for job, click download link
3. Save native XML/PDF files to output directory
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("zdrovena.month_closing.fakturownia_reports")

BASE_URL = "https://zdrovena.fakturownia.pl"
LOGIN_URL = f"{BASE_URL}/login"
DL_LINK_SEL = "#job_download_link a[href*='/jobs/']"


def _get_credentials() -> tuple[str, str]:
    """Resolve Fakturownia login/password from env vars or keyring."""
    import os
    from zdrovena.common.config import KEYCHAIN_ACCOUNT
    from zdrovena.month_closing.config import (
        KEYCHAIN_SERVICE_FAKTUROWNIA_LOGIN,
        KEYCHAIN_SERVICE_FAKTUROWNIA_PASSWORD,
    )

    login = os.environ.get("FAKTUROWNIA_LOGIN")
    password = os.environ.get("FAKTUROWNIA_PASSWORD")

    if not login:
        try:
            import keyring

            login = keyring.get_password(KEYCHAIN_SERVICE_FAKTUROWNIA_LOGIN, KEYCHAIN_ACCOUNT)
        except Exception:
            pass
    if not password:
        try:
            import keyring

            password = keyring.get_password(KEYCHAIN_SERVICE_FAKTUROWNIA_PASSWORD, KEYCHAIN_ACCOUNT)
        except Exception:
            pass

    if not login or not password:
        raise RuntimeError(
            "Fakturownia login credentials not found.\n"
            "Set FAKTUROWNIA_LOGIN and FAKTUROWNIA_PASSWORD env vars, or store in keyring."
        )
    return login, password


def download_fakturownia_reports(
    reports: list[dict],
    date_from: str,
    date_to: str,
    output_dir: Path,
    *,
    headless: bool = True,
    timeout: int = 120_000,
) -> list[tuple[dict, Path]]:
    """
    Download missing Fakturownia reports via headless browser.

    Parameters
    ----------
    reports : list of report config dicts (from FAKTUROWNIA_REPORTS)
    date_from, date_to : "YYYY-MM-DD"
    output_dir : directory to save downloaded files
    headless : run browser without visible window
    timeout : max wait per report in milliseconds

    Returns
    -------
    List of (report_config, downloaded_path) for successful downloads.
    """
    try:
        from playwright.sync_api import sync_playwright
        from playwright_stealth import Stealth
    except ImportError:
        logger.warning("Playwright not installed — skipping auto-download")
        return []

    login, password = _get_credentials()
    output_dir.mkdir(parents=True, exist_ok=True)
    downloaded: list[tuple[dict, Path]] = []

    logger.info("Launching browser (headless=%s) for Fakturownia reports...", headless)

    with Stealth().use_sync(sync_playwright()) as pw:
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        page.route("**/*consentmanager*", lambda route: route.abort())

        # ── Login ──
        try:
            _login(page, login, password)
        except Exception as exc:
            logger.error("Fakturownia login failed: %s", exc)
            browser.close()
            return []

        # ── Download each report ──
        for rpt in reports:
            try:
                path = _download_one_report(page, rpt, date_from, date_to, output_dir, timeout)
                if path:
                    downloaded.append((rpt, path))
            except Exception as exc:
                logger.warning("Failed to download %s: %s", rpt["name"], exc)

        browser.close()

    return downloaded


def _login(page, login: str, password: str) -> None:
    """Login to Fakturownia. Raises on failure."""
    logger.info("Logging in to Fakturownia...")
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    page.wait_for_selector("[name='user_session[login]']", timeout=10_000)
    page.fill("[name='user_session[login]']", login)
    page.fill("[name='user_session[password]']", password)
    page.click("[name='commit']", force=True)

    try:
        page.wait_for_url(
            lambda url: "login" not in url,
            wait_until="domcontentloaded",
            timeout=10_000,
        )
    except Exception as e:
        content = page.content()
        if "Nie masz uprawnień do tego konta!" in content:
            raise RuntimeError(f"Account '{login}' has no access to {BASE_URL}") from e
        if "Login/Hasło nie są poprawne" in content:
            raise RuntimeError(f"Invalid credentials for '{login}'") from e
        raise RuntimeError(f"Login timed out. URL: {page.url}") from e

    logger.info("Login OK → %s", page.url)


def _download_one_report(
    page, rpt: dict, date_from: str, date_to: str, output_dir: Path, timeout: int
) -> Path | None:
    """Navigate to report page, trigger generation, download the file."""
    name = rpt["name"]
    url = rpt["url"]
    dest_name = rpt["dest_name"]
    selector = rpt.get("download_selector", DL_LINK_SEL)

    params = (
        f"?date_from={date_from}&date_to={date_to}&submitted=true&currency_convert_to_main=false"
    )
    logger.info("Loading report page: %s %s", name, url)
    page.goto(url + params, wait_until="domcontentloaded")
    page.wait_for_timeout(2000)

    logger.info("Waiting for %s report job (timeout=%ds)...", name, timeout // 1000)
    try:
        page.wait_for_selector(selector, state="attached", timeout=timeout)
    except Exception:
        logger.warning("%s: report job timed out", name)
        return None

    # Click download link and capture the file
    output_path = output_dir / dest_name
    with page.expect_download(timeout=30_000) as download_info:
        page.click(selector)
    download = download_info.value
    download.save_as(str(output_path))

    size = output_path.stat().st_size
    if size < 100:
        logger.warning("%s: downloaded file too small (%d bytes), removing", name, size)
        output_path.unlink()
        return None

    logger.info("%s: saved %s (%d bytes)", name, output_path, size)
    return output_path
```

- [ ] **Step 2: Commit**

```bash
git add zdrovena/month_closing/fakturownia_reports.py
git commit -m "feat: add Playwright-based Fakturownia report auto-downloader"
```

---

### Task 2: Integrate into preflight

**Files:**
- Modify: `zdrovena/month_closing/preflight.py:271-297` (`_check_reports` method)
- Modify: `zdrovena/month_closing/commands/preflight_cmd.py:32-68` (add `--no-browser` flag)

- [ ] **Step 1: Add `--no-browser` flag to preflight CLI**

In `preflight_cmd.py`, add argument after the `--verbose` argument (line 63-66):

```python
    sp.add_argument(
        "--no-browser",
        action="store_true",
        help="Skip Playwright auto-download of Fakturownia reports",
    )
```

Pass it through to the checker in `_run()` at line 112:

```python
    no_browser = getattr(args, "no_browser", False)
    checker = PreflightChecker(
        year=year,
        month=month,
        month_dir=month_dir,
        date_from=date_from,
        date_to=date_to,
        cost_date_to=cost_date_to,
        dry_run=True,
        get_secret=_get_secret,
        no_browser=no_browser,
    )
```

- [ ] **Step 2: Add `no_browser` param to PreflightChecker**

In `preflight.py`, update `__init__` (line 37-56) to accept `no_browser`:

```python
    def __init__(
        self,
        year: int,
        month: int,
        month_dir: Path,
        date_from: str,
        date_to: str,
        cost_date_to: str,
        dry_run: bool,
        get_secret: object,
        no_browser: bool = False,
    ) -> None:
        # ... existing assignments ...
        self.no_browser = no_browser
```

- [ ] **Step 3: Add auto-download to `_check_reports()`**

Replace `_check_reports` method (lines 271-297) to attempt Playwright download for missing reports:

```python
    def _check_reports(self) -> None:
        watch_dir = DOWNLOAD_WATCH_DIR
        print("  ┌─ Fakturownia reports")

        # First pass: check what's already present
        missing: list[dict] = []
        for rpt in FAKTUROWNIA_REPORTS:
            dest = self.month_dir / rpt["dest_name"]
            if dest.exists():
                print(f"  │  ✅ {rpt['name']}: {dest.name} (in month folder)")
                continue
            if watch_dir.exists():
                matches = sorted(
                    watch_dir.glob(rpt["glob"]),
                    key=lambda f: f.stat().st_mtime,
                    reverse=True,
                )
                if matches:
                    newest = matches[0]
                    self.result.matches.append(
                        ({"name": rpt["name"], "dest_name": rpt["dest_name"]}, newest)
                    )
                    print(f"  │  ✅ {rpt['name']}: found {newest.name}")
                    continue
            missing.append(rpt)

        # Second pass: auto-download missing reports via Playwright
        if missing and not self.no_browser:
            try:
                from zdrovena.month_closing.fakturownia_reports import (
                    download_fakturownia_reports,
                )

                print(f"  │  🌐 Attempting auto-download of {len(missing)} report(s)...")
                downloaded = download_fakturownia_reports(
                    missing,
                    self.date_from,
                    self.date_to,
                    watch_dir,
                )
                for rpt_cfg, path in downloaded:
                    self.result.matches.append(
                        ({"name": rpt_cfg["name"], "dest_name": rpt_cfg["dest_name"]}, path)
                    )
                    print(f"  │  ✅ {rpt_cfg['name']}: auto-downloaded {path.name}")
                    missing = [r for r in missing if r["name"] != rpt_cfg["name"]]
            except Exception as exc:
                logger.warning("Auto-download failed: %s", exc)
                print(f"  │  ⚠️  Auto-download failed: {exc}")

        # Remaining missing reports → manual URLs
        for rpt in missing:
            self.result.missing_reports.append(rpt)
            print(f"  │  ⚠️  {rpt['name']}: not found in inbox/")
            if rpt.get("url"):
                print(f"  │     🔗 {rpt['url']}")
        print("  └─")
```

- [ ] **Step 4: Commit**

```bash
git add zdrovena/month_closing/preflight.py zdrovena/month_closing/commands/preflight_cmd.py
git commit -m "feat: integrate Fakturownia report auto-download into preflight"
```

---

### Task 3: Wire `--no-browser` through orchestrator

**Files:**
- Modify: `zdrovena/month_closing/orchestrator.py` (PreflightChecker instantiation)

- [ ] **Step 1: Pass `no_browser` from orchestrator to PreflightChecker**

Find where `PreflightChecker` is instantiated in `orchestrator.py` and add `no_browser=self.non_interactive`:

When running non-interactively (CI), browser downloads should be skipped. In interactive mode, attempt auto-download.

- [ ] **Step 2: Commit**

```bash
git add zdrovena/month_closing/orchestrator.py
git commit -m "feat: pass no_browser flag through orchestrator to preflight"
```

---

### Task 4: Add unit tests

**Files:**
- Create: `tests/test_fakturownia_reports.py`

- [ ] **Step 1: Write tests**

```python
"""Tests for zdrovena.month_closing.fakturownia_reports."""

from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch


class TestGetCredentials:
    def test_env_vars_used(self, monkeypatch):
        monkeypatch.setenv("FAKTUROWNIA_LOGIN", "user@test.com")
        monkeypatch.setenv("FAKTUROWNIA_PASSWORD", "secret")
        from zdrovena.month_closing.fakturownia_reports import _get_credentials

        login, pw = _get_credentials()
        assert login == "user@test.com"
        assert pw == "secret"

    def test_missing_credentials_raises(self, monkeypatch):
        monkeypatch.delenv("FAKTUROWNIA_LOGIN", raising=False)
        monkeypatch.delenv("FAKTUROWNIA_PASSWORD", raising=False)
        with patch("keyring.get_password", return_value=None):
            from zdrovena.month_closing.fakturownia_reports import _get_credentials

            with pytest.raises(RuntimeError, match="credentials not found"):
                _get_credentials()


class TestDownloadFakturowniaReports:
    def test_returns_empty_when_playwright_missing(self, tmp_path):
        """When playwright is not importable, returns empty list."""
        with patch.dict("sys.modules", {"playwright": None, "playwright.sync_api": None}):
            # Force reimport
            import importlib
            from zdrovena.month_closing import fakturownia_reports

            importlib.reload(fakturownia_reports)
            result = fakturownia_reports.download_fakturownia_reports(
                [{"name": "JPK_FA", "url": "http://x", "dest_name": "JPK_FA.xml"}],
                "2026-03-01",
                "2026-04-01",
                tmp_path,
            )
            assert result == []

    def test_returns_empty_on_login_failure(self, tmp_path, monkeypatch):
        """When login fails, returns empty list gracefully."""
        monkeypatch.setenv("FAKTUROWNIA_LOGIN", "bad@test.com")
        monkeypatch.setenv("FAKTUROWNIA_PASSWORD", "wrong")

        mock_page = MagicMock()
        mock_page.url = "https://zdrovena.fakturownia.pl/login"
        mock_page.content.return_value = "Login/Hasło nie są poprawne"
        mock_page.wait_for_url.side_effect = Exception("timeout")

        mock_context = MagicMock()
        mock_context.new_page.return_value = mock_page

        mock_browser = MagicMock()
        mock_browser.new_context.return_value = mock_context

        mock_pw = MagicMock()
        mock_pw.chromium.launch.return_value = mock_browser

        mock_stealth_ctx = MagicMock()
        mock_stealth_ctx.__enter__ = MagicMock(return_value=mock_pw)
        mock_stealth_ctx.__exit__ = MagicMock(return_value=False)

        with (
            patch(
                "zdrovena.month_closing.fakturownia_reports.Stealth", create=True
            ) as mock_stealth_cls,
            patch("zdrovena.month_closing.fakturownia_reports.sync_playwright", create=True),
        ):
            mock_stealth_cls.return_value.use_sync.return_value = mock_stealth_ctx

            import importlib
            from zdrovena.month_closing import fakturownia_reports

            importlib.reload(fakturownia_reports)
            result = fakturownia_reports.download_fakturownia_reports(
                [{"name": "JPK_FA", "url": "http://x", "dest_name": "JPK_FA.xml"}],
                "2026-03-01",
                "2026-04-01",
                tmp_path,
            )
            assert result == []


class TestPreflightNoBrowserFlag:
    def test_no_browser_skips_auto_download(self, tmp_path):
        """PreflightChecker with no_browser=True should not attempt download."""
        from zdrovena.month_closing.preflight import PreflightChecker

        checker = PreflightChecker(
            year=2026,
            month=3,
            month_dir=tmp_path / "month",
            date_from="2026-03-01",
            date_to="2026-04-01",
            cost_date_to="2026-04-01",
            dry_run=True,
            get_secret=lambda s, required=True: None,
            no_browser=True,
        )
        # Should not crash, should not attempt Playwright
        with patch("zdrovena.month_closing.preflight.DOWNLOAD_WATCH_DIR", tmp_path / "inbox"):
            result = checker.run()
        # All 3 reports should be missing (no auto-download attempted)
        assert len(result.missing_reports) == 3
```

- [ ] **Step 2: Run tests**

```bash
python3 -m pytest tests/test_fakturownia_reports.py -v
```

- [ ] **Step 3: Commit**

```bash
git add tests/test_fakturownia_reports.py
git commit -m "test: add unit tests for Fakturownia report auto-downloader"
```

---

### Task 5: Run full test suite and verify

- [ ] **Step 1: Run all tests**

```bash
python3 -m pytest -v
```

Expected: all tests pass including new ones.

- [ ] **Step 2: Verify CLI help**

```bash
python3 -c "from zdrovena.cli import main; import sys; sys.argv=['zdrovena', 'preflight', '--help']; main()"
```

Expected: `--no-browser` flag visible in help output.
