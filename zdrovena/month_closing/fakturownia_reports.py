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
    from zdrovena.month_closing.config import (
        FAKTUROWNIA_REPORT_RUNTIME,
        FAKTUROWNIA_REPORT_TIMEOUT_MS,
    )

    runtime = FAKTUROWNIA_REPORT_RUNTIME.strip().lower()
    resolved_timeout = timeout if timeout != 120_000 else FAKTUROWNIA_REPORT_TIMEOUT_MS
    if runtime == "playwright":
        return _download_reports_with_playwright(
            reports,
            date_from,
            date_to,
            output_dir,
            headless=headless,
            timeout=resolved_timeout,
        )

    logger.warning(
        "Unsupported report runtime '%s' — skipping auto-download (manual fallback remains)",
        runtime,
    )
    return []


def _download_reports_with_playwright(
    reports: list[dict],
    date_from: str,
    date_to: str,
    output_dir: Path,
    *,
    headless: bool,
    timeout: int,
) -> list[tuple[dict, Path]]:
    """Current runtime adapter: Playwright implementation."""
    try:
        from playwright.sync_api import sync_playwright
        from playwright_stealth import Stealth
    except ImportError:
        logger.warning("Playwright not installed — skipping auto-download")
        return []

    try:
        login, password = _get_credentials()
    except Exception as exc:
        logger.warning("Fakturownia credentials unavailable — skipping auto-download: %s", exc)
        return []
    output_dir.mkdir(parents=True, exist_ok=True)
    downloaded: list[tuple[dict, Path]] = []

    logger.info("Launching browser (headless=%s) for Fakturownia reports...", headless)

    with Stealth().use_sync(sync_playwright()) as pw:
        try:
            browser = pw.chromium.launch(headless=headless)
        except Exception as exc:
            logger.warning("Unable to launch Playwright browser — skipping auto-download: %s", exc)
            return []
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        page.route("**/*consentmanager*", lambda route: route.abort())

        try:
            _login(page, login, password)
        except Exception as exc:
            logger.error("Fakturownia login failed: %s", exc)
            browser.close()
            return []

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
    page,
    rpt: dict,
    date_from: str,
    date_to: str,
    output_dir: Path,
    timeout: int,
) -> Path | None:
    """Navigate to report page, trigger generation, download the file."""
    name = rpt["name"]
    url = rpt["url"]
    dest_name = rpt["dest_name"]
    from zdrovena.month_closing.config import FAKTUROWNIA_REPORT_DOWNLOAD_SELECTOR

    selector = rpt.get("download_selector", FAKTUROWNIA_REPORT_DOWNLOAD_SELECTOR or DL_LINK_SEL)

    params = (
        f"?date_from={date_from}&date_to={date_to}"
        f"&submitted=true&currency_convert_to_main=false"
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
