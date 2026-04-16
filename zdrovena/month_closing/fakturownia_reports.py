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
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

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
            if "Executable doesn't exist" in str(exc):
                logger.warning(
                    "Playwright browser binary missing. Install with: "
                    ".venv/bin/python -m playwright install chromium"
                )
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
    button_texts: list[str] = rpt.get("download_button_texts", [])
    append_date_params = rpt.get("append_date_params", True)
    use_wizard_navigation = bool(rpt.get("use_wizard_navigation", False))

    params = (
        f"?date_from={date_from}&date_to={date_to}"
        f"&submitted=true&currency_convert_to_main=false"
    )
    target_url = _build_report_url(url, params, append_date_params=append_date_params)
    logger.info("Loading report page: %s %s", name, target_url)
    page.goto(target_url, wait_until="domcontentloaded")
    page.wait_for_timeout(2000)
    output_path = output_dir / dest_name
    if use_wizard_navigation and _try_v7_wizard_download(page, output_path, timeout):
        size = output_path.stat().st_size
        if size < 100:
            logger.warning("%s: generated file too small (%d bytes), removing", name, size)
            output_path.unlink()
            return None
        logger.info("%s: saved %s (%d bytes)", name, output_path, size)
        return output_path
    if _try_download_by_button_text(page, output_path, timeout, button_texts):
        size = output_path.stat().st_size
        if size < 100:
            logger.warning("%s: generated file too small (%d bytes), removing", name, size)
            output_path.unlink()
            return None
        logger.info("%s: saved %s (%d bytes)", name, output_path, size)
        return output_path
    if _try_generate_and_download(page, output_path, timeout):
        size = output_path.stat().st_size
        if size < 100:
            logger.warning("%s: generated file too small (%d bytes), removing", name, size)
            output_path.unlink()
            return None
        logger.info("%s: saved %s (%d bytes)", name, output_path, size)
        return output_path
    if _try_download_by_button_text(page, output_path, timeout, button_texts):
        size = output_path.stat().st_size
        if size < 100:
            logger.warning("%s: generated file too small (%d bytes), removing", name, size)
            output_path.unlink()
            return None
        logger.info("%s: saved %s (%d bytes)", name, output_path, size)
        return output_path

    logger.info("Waiting for %s report job (timeout=%ds)...", name, timeout // 1000)
    try:
        page.wait_for_selector(selector, state="attached", timeout=timeout)
    except Exception:
        # Fakturownia can expose job_id in URL/query before rendering standard selector.
        logger.warning("%s: report selector timed out, trying job-id fallback", name)

    try:
        with page.expect_download(timeout=30_000) as download_info:
            page.click(selector)
        download = download_info.value
        download.save_as(str(output_path))
    except Exception as exc:
        logger.warning("%s: click-based download failed (%s), trying direct job URL", name, exc)
        if not _download_via_job_url(page, selector, output_path, timeout_ms=timeout):
            return None

    size = output_path.stat().st_size
    if size < 100:
        logger.warning("%s: downloaded file too small (%d bytes), removing", name, size)
        output_path.unlink()
        return None

    logger.info("%s: saved %s (%d bytes)", name, output_path, size)
    return output_path


def _build_report_url(base_url: str, params: str, *, append_date_params: bool) -> str:
    if not append_date_params:
        return base_url
    if "?" in base_url:
        return base_url + "&" + params.lstrip("?")
    return base_url + params


def _try_download_by_button_text(
    page,
    output_path: Path,
    timeout_ms: int,
    button_texts: list[str],
) -> bool:
    if _try_v7_generate_then_download(page, output_path, timeout_ms):
        return True
    _accept_pouczenia_if_present(page, button_texts)
    for label in button_texts:
        try:
            if page.get_by_text(label, exact=False).count() == 0:
                continue
            with page.expect_download(timeout=timeout_ms) as download_info:
                page.get_by_text(label, exact=False).first.click(force=True, timeout=5000)
            download = download_info.value
            download.save_as(str(output_path))
            return True
        except Exception:
            continue
    return False


def _try_v7_generate_then_download(page, output_path: Path, timeout_ms: int) -> bool:
    """VAT V7 flow: accept consent, generate XML, then click download button."""
    # This flow mirrors the verified manual sequence from Playwright Inspector:
    # checkbox -> "zapisz i generuj xml" -> "pobierz xml".
    try:
        if page.get_by_role("button", name=re.compile(r"zapisz i generuj xml", re.I)).count() == 0:
            return False
    except Exception:
        return False

    try:
        _accept_pouczenia_if_present(page, ["pobierz xml", "zapisz i generuj xml"])
        page.get_by_role("button", name=re.compile(r"zapisz i generuj xml", re.I)).first.click(
            force=True,
            timeout=10_000,
        )
        with page.expect_download(timeout=timeout_ms) as download_info:
            page.get_by_role("button", name=re.compile(r"pobierz xml", re.I)).first.click(
                force=True,
                timeout=10_000,
            )
        download = download_info.value
        download.save_as(str(output_path))
        return True
    except Exception:
        return False


def _try_v7_wizard_download(page, output_path: Path, timeout_ms: int) -> bool:
    """VAT V7 path verified in UI: navigate wizard and download XML."""
    try:
        # These steps are resilient no-ops when the link/button is absent.
        _safe_click_role(page, "link", r"Raporty")
        _safe_click_role(page, "link", r"Moje JPK")
        _safe_click_role(page, "link", r"Nowy JPK V7")
        _safe_click_role(page, "button", r"Nowy raport")
        _accept_pouczenia_if_present(page, ["pobierz xml", "zapisz i generuj xml"])
        _safe_click_role(page, "button", r"zapisz i generuj xml", required=True)
        with page.expect_download(timeout=timeout_ms) as download_info:
            _safe_click_role(page, "button", r"pobierz xml", required=True)
        download = download_info.value
        download.save_as(str(output_path))
        return True
    except Exception:
        return False


def _safe_click_role(page, role: str, name_pattern: str, *, required: bool = False) -> bool:
    locator = page.get_by_role(role, name=re.compile(name_pattern, re.I))
    if locator.count() == 0:
        if required:
            raise RuntimeError(f"Required {role} '{name_pattern}' not found")
        return False
    locator.first.click(force=True, timeout=10_000)
    page.wait_for_timeout(300)
    return True


def _accept_pouczenia_if_present(page, button_texts: list[str]) -> None:
    """JPK_V7 pages can require consent checkbox before XML button is enabled."""
    lowered = [b.strip().lower() for b in button_texts]
    needs_consent = any("pobierz xml" in b or "zapisz i generuj xml" in b for b in lowered)
    if not needs_consent:
        return
    try:
        checkbox = page.locator("input[type='checkbox']").first
        if page.locator("input[type='checkbox']").count() == 0:
            return
        if not checkbox.is_checked():
            checkbox.click(force=True, timeout=5000)
            page.wait_for_timeout(500)
    except Exception:
        # If consent UI is absent or custom-wired, keep fallback behavior.
        return


def _extract_job_result_href(page, selector: str) -> str | None:
    """Get /jobs/{id}/result href from DOM or URL query."""
    href: str | None = None
    try:
        href = page.locator(selector).first.get_attribute("href")
    except Exception:
        href = None
    if not href:
        # Fallback: broader anchor lookup, used by some report pages.
        try:
            href = page.locator("a[href*='/jobs/'][href$='/result']").first.get_attribute("href")
        except Exception:
            href = None
    if href:
        return href

    # Final fallback: derive from ?job_id=... URL query.
    parsed = urlparse(page.url)
    job_id = parse_qs(parsed.query).get("job_id", [None])[0]
    if not job_id:
        m = re.search(r"/jobs/(\d+)/", parsed.path)
        job_id = m.group(1) if m else None
    if job_id:
        return f"/jobs/{job_id}/result"
    return None


def _has_job_link(page) -> bool:
    try:
        return page.locator("a[href*='/jobs/']").count() > 0
    except Exception:
        return False


def _try_generate_and_download(page, output_path: Path, timeout_ms: int) -> bool:
    """Try direct download path triggered by 'Generuj raport' submit."""
    if _has_job_link(page):
        return False

    # Most report pages use input[name='commit'] with label "Generuj raport".
    for generate_selector in (
        "input[name='commit']",
        "button[type='submit']",
    ):
        try:
            if page.locator(generate_selector).count() == 0:
                continue
            with page.expect_download(timeout=timeout_ms) as download_info:
                page.click(generate_selector, force=True, timeout=5000)
            download = download_info.value
            download.save_as(str(output_path))
            return True
        except Exception:
            continue
    return False


def _download_via_job_url(page, selector: str, output_path: Path, *, timeout_ms: int) -> bool:
    """Fallback: poll for job URL and fetch authenticated result endpoint."""
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    href = _extract_job_result_href(page, selector)
    while not href and time.monotonic() < deadline:
        href = _extract_job_result_href(page, selector)
        if href:
            break
        page.wait_for_timeout(2000)

    if not href:
        logger.warning("Unable to resolve report job link from DOM or URL")
        return False
    if not href:
        return False
    download_url = href if href.startswith("http") else f"{BASE_URL}{href}"
    try:
        response = page.context.request.get(download_url, timeout=30_000)
    except Exception as exc:
        logger.warning("Direct report download request failed: %s", exc)
        return False
    if not response.ok:
        logger.warning("Direct report download HTTP error for %s", download_url)
        return False
    output_path.write_bytes(response.body())
    return True
