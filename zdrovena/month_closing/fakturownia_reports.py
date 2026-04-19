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

    # Manual-session reports require a visible browser window.
    if any(r.get("use_wizard_navigation") for r in reports):
        headless = False

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

        # Separate wizard reports (combined sidebar session) from legacy reports
        wizard_reports = [r for r in reports if r.get("use_wizard_navigation")]
        legacy_reports = [r for r in reports if not r.get("use_wizard_navigation")]

        # All three standard reports use wizard navigation — run them together
        if wizard_reports:
            try:
                results = _run_all_wizard_reports(
                    page, wizard_reports, date_from, date_to, output_dir, timeout
                )
                downloaded.extend(results)
            except Exception as exc:
                logger.warning("Combined wizard session failed: %s", exc)

        # Legacy non-wizard reports (if any remain)
        for rpt in legacy_reports:
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
        f"?date_from={date_from}&date_to={date_to}&submitted=true&currency_convert_to_main=false"
    )
    target_url = _build_report_url(url, params, append_date_params=append_date_params)
    logger.info("Loading report page: %s %s", name, target_url)
    page.goto(target_url, wait_until="domcontentloaded")
    page.wait_for_timeout(2000)
    output_path = output_dir / dest_name
    if use_wizard_navigation:
        if not _run_manual_browser_session(page, rpt, output_path, timeout):
            return None
        size = output_path.stat().st_size
        if size < 100:
            logger.warning("%s: generated file too small (%d bytes), removing", name, size)
            output_path.unlink()
            return None
        logger.info("%s: saved %s (%d bytes)", name, output_path, size)
        return output_path

    # For non-wizard reports the XML also comes via the ksiegosoft SPA fetching
    # a pre-signed S3 URL and triggering a blob: download in Chrome.
    # Playwright's expect_download does NOT fire for blob: downloads.
    # page.route() with route.fetch() captures the body before Chrome discards it.
    import re as _re

    non_wizard_captured: list[bytes] = []

    def _intercept_xml_nw(route, req) -> None:
        try:
            response = route.fetch()
            ct = response.headers.get("content-type", "")
            cd = response.headers.get("content-disposition", "")
            body = response.body()
            if len(body) > 500 and (
                "xml" in ct.lower() or ("octet-stream" in ct.lower() and "xml" in cd.lower())
            ):
                print(f"  📡  [nw route captured XML] {req.url[:70]!r} ({len(body)} bytes)")
                non_wizard_captured.append(body)
            route.fulfill(response=response)
        except Exception as exc:
            print(f"  ⚠️  nw route intercept error: {exc}")
            try:
                route.continue_()
            except Exception:
                pass

    _s3_pattern = _re.compile(r"amazonaws\.com")
    page.route(_s3_pattern, _intercept_xml_nw)
    if _try_download_by_button_text(page, output_path, timeout, button_texts):
        # The route may have captured the real body before expect_download saved HTML.
        if non_wizard_captured:
            output_path.write_bytes(non_wizard_captured[-1])
            logger.info(
                "%s: overwritten with route-captured body (%d bytes)",
                name,
                len(non_wizard_captured[-1]),
            )
        size = output_path.stat().st_size
        if size < 100:
            logger.warning("%s: generated file too small (%d bytes), removing", name, size)
            output_path.unlink()
            page.unroute(_s3_pattern, _intercept_xml_nw)
            return None
        logger.info("%s: saved %s (%d bytes)", name, output_path, size)
        page.unroute(_s3_pattern, _intercept_xml_nw)
        return output_path
    if _try_generate_and_download(page, output_path, timeout, button_texts=button_texts):
        if non_wizard_captured:
            output_path.write_bytes(non_wizard_captured[-1])
            logger.info(
                "%s: overwritten with route-captured body (%d bytes)",
                name,
                len(non_wizard_captured[-1]),
            )
        size = output_path.stat().st_size
        if size < 100:
            logger.warning("%s: generated file too small (%d bytes), removing", name, size)
            output_path.unlink()
            page.unroute(_s3_pattern, _intercept_xml_nw)
            return None
        logger.info("%s: saved %s (%d bytes)", name, output_path, size)
        page.unroute(_s3_pattern, _intercept_xml_nw)
        return output_path

    logger.info("Waiting for %s report job (timeout=%ds)...", name, timeout // 1000)
    try:
        page.wait_for_selector(selector, state="attached", timeout=timeout)
    except Exception:
        logger.warning("%s: report selector timed out, trying job-id fallback", name)

    # Click the job link; the S3 route will capture the XML body.
    try:
        page.click(selector, force=True, timeout=10_000)
    except Exception:
        pass
    page.wait_for_timeout(3000)

    if non_wizard_captured:
        output_path.write_bytes(non_wizard_captured[-1])
        logger.info("%s: saved via route intercept (%d bytes)", name, len(non_wizard_captured[-1]))
    elif not output_path.exists() or output_path.stat().st_size < 100:
        # Last resort: authenticated HTTP fetch
        if not _download_via_job_url(page, selector, output_path, timeout_ms=30_000):
            logger.warning("%s: all download strategies failed", name)
            page.unroute(_s3_pattern, _intercept_xml_nw)
            return None

    page.unroute(_s3_pattern, _intercept_xml_nw)
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


def _build_multi_sidebar_js(reports_data: list[tuple[str, str, str]]) -> str:
    """Build JS that injects a multi-report sidebar showing all reports at once.

    reports_data: list of (name, url, dest_name) tuples.
    Each row has Go→ (navigates to that report's URL) and Skip⏭ buttons.
    Python polls sessionStorage keys __zdv_done_N and __zdv_skipped_N.
    """
    rows_js = ""
    for i, (name, url, dest_name) in enumerate(reports_data):
        safe_name = name.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "")
        safe_url = url.replace("\\", "\\\\").replace("'", "\\'")
        safe_dest = dest_name.replace("\\", "\\\\").replace("\n", "")
        rows_js += f"""
    (function() {{
        var row = document.createElement('div');
        row.id = '__zdv_sb_row_{i}';
        row.style.cssText = 'background:#252540;border-radius:6px;padding:10px;margin-bottom:8px;border:1px solid transparent';

        var hdr = document.createElement('div');
        hdr.style.cssText = 'font-weight:600;margin-bottom:6px;display:flex;align-items:center;gap:6px';
        var st = document.createElement('span');
        st.id = '__zdv_sb_status_{i}';
        st.textContent = '\\u23f3';
        var nm = document.createElement('span');
        nm.textContent = '{safe_name}';
        hdr.appendChild(st); hdr.appendChild(nm);
        row.appendChild(hdr);

        var dest = document.createElement('div');
        dest.style.cssText = 'font-size:11px;color:#888;margin-bottom:8px';
        dest.textContent = '\\u2192 {safe_dest}';
        row.appendChild(dest);

        var br = document.createElement('div');
        br.style.cssText = 'display:flex;gap:6px';

        var go = document.createElement('button');
        go.style.cssText = 'flex:1;padding:6px;background:#4f46e5;color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:12px;font-weight:600';
        go.textContent = 'Go \\u2192';
        go.onclick = function() {{ __zdv_ck_set('__zdv_active','{i}'); window.location.href='{safe_url}'; }};

        var skip = document.createElement('button');
        skip.id = '__zdv_sb_skip_{i}';
        skip.style.cssText = 'padding:6px 10px;background:#374151;color:#9ca3af;border:none;border-radius:4px;cursor:pointer;font-size:12px';
        skip.textContent = 'Skip \\u23ed';
        skip.onclick = function() {{ __zdv_ck_set('__zdv_skipped_{i}','true'); skip.textContent='Skipped'; skip.style.background='#78350f'; skip.style.color='#fbbf24'; }};

        br.appendChild(go); br.appendChild(skip);
        row.appendChild(br);
        sb.appendChild(row);
    }})();
"""
    return f"""(function() {{
    function __zdv_ck_get(k) {{ var m = document.cookie.match('(?:^|; )'+k+'=([^;]*)'); return m ? decodeURIComponent(m[1]) : null; }}
    function __zdv_ck_set(k,v) {{ document.cookie = k+'='+encodeURIComponent(v)+'; path=/; SameSite=Lax; max-age=3600'; }}

    if (document.getElementById('__zdv_sb')) {{
        // Sidebar already present: restore cookie state on re-injection.
        for (var _i = 0; _i < {len(reports_data)}; _i++) {{
            if (__zdv_ck_get('__zdv_done_'+_i) === 'true') {{
                var _s = document.getElementById('__zdv_sb_status_'+_i);
                var _r = document.getElementById('__zdv_sb_row_'+_i);
                if (_s) _s.textContent = '\\u2705';
                if (_r) {{ _r.style.background='#1a3a1a'; _r.style.borderColor='#4caf50'; }}
            }}
            if (__zdv_ck_get('__zdv_skipped_'+_i) === 'true') {{
                var _b = document.getElementById('__zdv_sb_skip_'+_i);
                if (_b) {{ _b.textContent='Skipped'; _b.style.background='#78350f'; _b.style.color='#fbbf24'; }}
            }}
        }}
        return;
    }}

    window.__zdv_mark_done = function(i) {{
        __zdv_ck_set('__zdv_done_' + i, 'true');
        var s = document.getElementById('__zdv_sb_status_' + i);
        var r = document.getElementById('__zdv_sb_row_' + i);
        if (s) s.textContent = '\\u2705';
        if (r) {{ r.style.background='#1a3a1a'; r.style.border='1px solid #4caf50'; }}
    }};

    var sb = document.createElement('div');
    sb.id = '__zdv_sb';
    sb.style.cssText = 'position:fixed;right:0;top:0;width:240px;height:100vh;background:#1a1a2e;color:#fff;z-index:2147483647;padding:14px 10px;box-sizing:border-box;font-family:system-ui,sans-serif;font-size:13px;overflow-y:auto;border-left:2px solid #3a3a5e;box-shadow:-4px 0 12px rgba(0,0,0,.5)';

    var t = document.createElement('div');
    t.style.cssText = 'font-weight:700;font-size:14px;margin-bottom:14px;padding-bottom:8px;border-bottom:1px solid #3a3a5e;color:#a78bfa';
    t.textContent = '\\ud83d\\udccb Zdrovena Reports';
    sb.appendChild(t);

    {rows_js}

    // Green "Done" button — closes session with success
    var doneBtn = document.createElement('button');
    doneBtn.id = '__zdv_sb_all_done';
    doneBtn.style.cssText = 'width:100%;padding:10px;background:#16a34a;color:#fff;border:none;border-radius:6px;cursor:pointer;font-size:14px;font-weight:700;margin-top:8px';
    doneBtn.textContent = '\u2705 Done — close session';
    doneBtn.onclick = function() {{
        doneBtn.disabled = true;
        doneBtn.textContent = '\u2705 Closing...';
        doneBtn.style.background = '#15803d';
        __zdv_ck_set('__zdv_all_done', 'true');
    }};
    sb.appendChild(doneBtn);

    document.body.appendChild(sb);

    // Restore cookie state NOW — elements are in the DOM
    for (var _ri = 0; _ri < {len(reports_data)}; _ri++) {{
        if (__zdv_ck_get('__zdv_done_'+_ri) === 'true') {{
            var _rs = document.getElementById('__zdv_sb_status_'+_ri);
            var _rr = document.getElementById('__zdv_sb_row_'+_ri);
            if (_rs) _rs.textContent = '\u2705';
            if (_rr) {{ _rr.style.background='#1a3a1a'; _rr.style.border='1px solid #4caf50'; }}
        }}
        if (__zdv_ck_get('__zdv_skipped_'+_ri) === 'true') {{
            var _rb = document.getElementById('__zdv_sb_skip_'+_ri);
            if (_rb) {{ _rb.textContent='Skipped'; _rb.style.background='#78350f'; _rb.style.color='#fbbf24'; }}
        }}
    }}
}})();"""


def _build_sidebar_js(name: str, url: str, dest_name: str) -> str:
    """Single-report sidebar — kept for backwards compatibility."""
    return _build_multi_sidebar_js([(name, url, dest_name)])


def _auto_fill_jpk_v7m(page) -> None:
    """Best-effort: click Nowy raport → accept consent → zapisz i generuj xml → pobierz xml.

    Silently returns on any failure so the manual sidebar session takes over.
    """
    try:
        # 1. Click "Nowy raport" if present (we may already be on a report page)
        nowy = page.get_by_role("button", name=re.compile(r"nowy raport", re.I))
        if nowy.count() > 0:
            print("  🤖  Auto: clicking 'Nowy raport'...")
            nowy.first.click(force=True, timeout=8_000)
            page.wait_for_timeout(3000)
        else:
            print("  🤖  Auto: 'Nowy raport' not found — skipping (may already be on report page)")

        # 2. Accept consent checkbox
        cb = page.locator("input[type='checkbox']")
        if cb.count() > 0 and not cb.first.is_checked():
            print("  🤖  Auto: accepting consent checkbox...")
            cb.first.click(force=True, timeout=5_000)
            page.wait_for_timeout(500)

        # 3. Click "zapisz i generuj xml"
        gen = page.get_by_role("button", name=re.compile(r"zapisz i generuj xml", re.I))
        if gen.count() == 0:
            print("  🤖  Auto: 'zapisz i generuj xml' not found — user must complete form manually")
            return
        print("  🤖  Auto: clicking 'zapisz i generuj xml'...")
        gen.first.click(force=True, timeout=10_000)
        page.wait_for_timeout(3000)

        # 4. Wait for and click "pobierz xml" (report generation may take a few seconds)
        print("  🤖  Auto: waiting for 'pobierz xml'...")
        for _ in range(12):  # up to 60s
            pobierz = page.get_by_role("button", name=re.compile(r"pobierz xml", re.I))
            if pobierz.count() > 0:
                print("  🤖  Auto: clicking 'pobierz xml'...")
                pobierz.first.click(force=True, timeout=10_000)
                return
            page.wait_for_timeout(5000)
        print("  🤖  Auto: 'pobierz xml' did not appear — user must click manually")
    except Exception as exc:
        print(f"  🤖  Auto form-fill failed ({exc}) — complete manually in browser")


def _auto_fill_vat_register(page) -> None:
    """VAT Sales Register: click 'Generuj raport' to start the async job.

    Unlike JPK_FA, the income_tax_records page does NOT auto-submit the job on
    load — the user (or automation) must click 'Generuj raport'.
    After the job completes the page's JS navigates to S3 with
    Content-Disposition: attachment. The response listener in
    _run_manual_browser_session captures that URL and re-fetches it.
    """
    try:
        # The submit button is input[name='commit'] labelled 'Generuj raport'.
        btn = page.locator("input[name='commit'], button[type='submit']")
        if btn.count() > 0:
            print("  🤖  VAT Register: clicking 'Generuj raport'...")
            btn.first.click(force=True, timeout=8_000)
            page.wait_for_timeout(1000)
        else:
            print("  🤖  VAT Register: 'Generuj raport' not found — complete manually")
    except Exception as exc:
        print(f"  🤖  VAT Register auto-fill failed ({exc}) — complete manually")


def _auto_fill_jpk_fa(page) -> None:
    """JPK_FA download is triggered automatically by the page JS after job completion.

    The URL is loaded with submitted=true which starts the Fakturownia backend job.
    JavaScript on the result page then auto-navigates to /reports/jpk_fa.xml → S3.
    There is no button to click — the download fires at ~8-10s without user input.
    The response listener in _run_manual_browser_session captures the S3 URL.

    We briefly yield to Playwright's event loop so the response event that fires
    during auto-navigation is processed before we return to the poll loop.
    """
    # The page is loaded with submitted=true — Fakturownia JS will automatically
    # poll /jobs/worker.json, then navigate to /reports/jpk_fa.xml → S3.
    # No action needed here; the response listener in _run_manual_browser_session
    # will capture the S3 URL.  Control is given to the user immediately.
    print("  🤖  JPK_FA: browser ready — waiting for auto-download or your action...")


def _auto_fill_for_report(page, rpt: dict) -> None:
    """Dispatch the right auto-fill function for a given report."""
    name = rpt["name"]
    if name == "JPK_FA":
        _auto_fill_jpk_fa(page)
    elif name == "VAT Sales Register":
        _auto_fill_vat_register(page)
    else:
        _auto_fill_jpk_v7m(page)


def _run_all_wizard_reports(
    page,
    reports: list[dict],
    date_from: str,
    date_to: str,
    output_dir: Path,
    timeout_ms: int,
) -> list[tuple[dict, Path]]:
    """Run all wizard-navigation reports in a single browser session.

    Opens one browser tab with a multi-report sidebar showing all reports.
    Navigates to each report URL in turn, auto-fills the form, and captures
    S3 responses via page.on("response").  The poll loop re-fetches each
    captured S3 URL via page.context.request.get() and saves the result.

    The sidebar lets the user skip any report or trigger a report manually
    via the Go → button.  Session ends when all reports are done/skipped or
    the timeout is reached.

    Returns list of (report_config, path) for successfully downloaded reports.
    """

    # Build per-report metadata
    reports_data = []
    output_paths = []
    for rpt in reports:
        url = rpt["url"]
        dest_name = rpt["dest_name"]
        name = rpt["name"]
        append = rpt.get("append_date_params", True)
        params = (
            f"?date_from={date_from}&date_to={date_to}"
            f"&submitted=true&currency_convert_to_main=false"
        )
        nav_url = _build_report_url(url, params, append_date_params=append)
        reports_data.append((name, nav_url, dest_name))
        output_paths.append(output_dir / dest_name)

    n = len(reports)
    sidebar_js = _build_multi_sidebar_js(reports_data)

    # One shared response listener for all reports — stores (idx, s3_url) pairs
    pending_s3: list[tuple[int, str, int]] = []  # (report_idx, url, capture_serial)
    _serial: list[int] = [0]

    # Maps captured S3 URLs to which report index they belong.
    # We track the "active" index set when Go→ is clicked (or sequentially).
    active_idx: list[int] = [0]

    def _on_resp(resp) -> None:
        cd = resp.headers.get("content-disposition", "")
        ct = resp.headers.get("content-type", "")
        url = resp.url
        if "amazonaws.com" in url:
            print(f"  🌐  [aws {resp.status}] ct={ct!r}  cd={cd!r}")
            print(f"       url={url[:90]!r}")
        if "amazonaws.com" in url and "attachment" in cd.lower():
            idx = active_idx[0]
            serial = _serial[0]
            _serial[0] += 1
            print(f"  📡  [captured idx={idx}] {url[:80]!r}")
            pending_s3.append((idx, url, serial))

    page.on("response", _on_resp)

    def _inject_sidebar() -> None:
        """Re-inject sidebar if absent (safe to call from poll loop)."""
        try:
            has_sb = page.evaluate("!!document.getElementById('__zdv_sb')")
            if not has_sb:
                page.evaluate(sidebar_js)
        except Exception:
            pass

    # Navigate to first report and inject sidebar
    first_name, first_url, _ = reports_data[0]
    print(f"\n  ▶  Navigating to {first_name}...")
    page.goto(first_url, wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    _inject_sidebar()

    # Auto-fill first report
    _auto_fill_for_report(page, reports[0])

    print(
        f"\n  🌐  Browser open — {n} report(s) in sidebar\n"
        f"     Files will be saved to: {output_dir}\n"
        f"     Use Go → to navigate, Skip ⏭ to skip a report.\n"
    )

    done: dict[int, Path] = {}  # idx → path
    skipped: set[int] = set()
    poll_n = 0
    current_idx = 0
    _all_notified = False
    # No deadline — user closes via the green Done button or by closing the browser.

    while True:
        try:
            page.wait_for_timeout(500)
        except Exception:
            # Browser was closed by the user
            print("  🔒  Browser closed — ending session")
            break
        poll_n += 1

        # Read active index from cookie (user may have clicked Go→)
        try:
            val = page.evaluate(
                "(function(){var m=document.cookie.match(/(?:^|; )__zdv_active=([^;]*)/);return m?decodeURIComponent(m[1]):null;})()"
            )
            if val is not None:
                active_idx[0] = int(val)
        except Exception:
            pass

        # Auto-navigate to next pending report when current one completes/skips
        if current_idx < n and current_idx not in done and current_idx not in skipped:
            pass  # still working on current_idx
        else:
            # Find next unattempted report
            next_idx = None
            for i in range(n):
                if i not in done and i not in skipped:
                    next_idx = i
                    break
            if next_idx is not None and next_idx != current_idx:
                current_idx = next_idx
                active_idx[0] = next_idx
                rpt = reports[next_idx]
                nav_url = reports_data[next_idx][1]
                print(f"\n  ▶  Auto-navigating to {rpt['name']}...")
                try:
                    page.goto(nav_url, wait_until="domcontentloaded")
                    page.wait_for_timeout(1500)
                    _inject_sidebar()
                    _auto_fill_for_report(page, rpt)
                except Exception as exc:
                    print(f"  ⚠️  Navigation to {rpt['name']} failed: {exc}")

        # Re-inject sidebar whenever it's missing (e.g. after Go→ navigation)
        _inject_sidebar()

        if poll_n % 10 == 0:
            remaining = [reports[i]["name"] for i in range(n) if i not in done and i not in skipped]
            print(
                f"  ⏳  ({poll_n // 2}s) done={len(done)}/{n}  pending_s3={len(pending_s3)}  remaining={remaining}"
            )

        # Process captured S3 URLs
        for item in list(pending_s3):
            idx, s3_url, _serial = item
            pending_s3.remove(item)
            out_path = output_paths[idx]
            rpt_name = reports[idx]["name"]
            try:
                print(f"  🔄  Re-fetching [{rpt_name}] …{s3_url[-50:]!r}")
                api_resp = page.context.request.get(s3_url, timeout=30_000)
                if api_resp.ok:
                    body = api_resp.body()
                    if len(body) > 500:
                        out_path.write_bytes(body)
                        print(f"  📥  Saved [{rpt_name}] ({len(body)} bytes) → {out_path}")
                        logger.info("%s: saved %s (%d bytes)", rpt_name, out_path, len(body))
                    else:
                        print(f"  ⚠️  [{rpt_name}] S3 body too small ({len(body)}b), skipping")
                else:
                    print(f"  ⚠️  [{rpt_name}] S3 re-fetch HTTP {api_resp.status}")
            except Exception as exc:
                print(f"  ❌  [{rpt_name}] S3 re-fetch failed: {exc}")
                logger.warning("%s: S3 re-fetch error: %s", rpt_name, exc)

        # Mark done when file appears
        for i in range(n):
            if i in done or i in skipped:
                continue
            p = output_paths[i]
            if p.exists() and p.stat().st_size >= 100:
                done[i] = p
                try:
                    page.evaluate(f"window.__zdv_mark_done({i})")
                except Exception:
                    pass
                print(f"  ✅  [{reports[i]['name']}] complete")
                logger.info(
                    "%s: download complete (%d bytes)", reports[i]["name"], p.stat().st_size
                )

        # Check skipped via cookie
        for i in range(n):
            if i in done or i in skipped:
                continue
            try:
                if page.evaluate(
                    f"(function(){{var m=document.cookie.match(/(?:^|; )__zdv_skipped_{i}=([^;]*)/);return m?decodeURIComponent(m[1])==='true':false;}})()"
                ):
                    skipped.add(i)
                    print(f"  ⏭  [{reports[i]['name']}] skipped by user")
                    logger.info("%s: skipped by user", reports[i]["name"])
            except Exception:
                pass

        # All accounted for? — notify user and highlight Done button, but don't auto-close
        if len(done) + len(skipped) >= n and not _all_notified:
            _all_notified = True
            print("  ✅  All reports complete — click ✅ Done button to close session")
            try:
                page.evaluate("""
                    var b = document.getElementById('__zdv_sb_all_done');
                    if (b && !b.disabled) {
                        b.textContent = '\u2705 All done \u2014 click to close';
                        b.style.outline = '3px solid #bbf7d0';
                        b.style.boxShadow = '0 0 14px #16a34a';
                    }
                """)
            except Exception:
                pass

        # User clicked the green Done button (or browser closed)
        try:
            if page.evaluate(
                "(function(){var m=document.cookie.match(/(?:^|; )__zdv_all_done=([^;]*)/);return m?decodeURIComponent(m[1])==='true':false;})()"
            ):
                print("  ✅  Done button pressed — closing session")
                break
        except Exception:
            # Browser closed
            print("  🔒  Browser closed — ending session")
            break

    # Cleanup
    try:
        page.remove_listener("response", _on_resp)
    except Exception:
        pass

    return [(reports[i], done[i]) for i in sorted(done)]


def _run_manual_browser_session(
    page,
    rpt: dict,
    output_path: Path,
    timeout_ms: int,
) -> bool:
    """Single-report wizard session (kept for standalone use).

    Used when _download_one_report is called for a single wizard report outside
    the combined _run_all_wizard_reports flow.
    Returns True if the file was downloaded, False if skipped or timed out.
    """
    name = rpt["name"]
    url = rpt["url"]
    dest_name = rpt["dest_name"]

    # Two download patterns exist:
    #
    # JPK_V7M (ksiegosoft SPA): SPA calls fetch() → S3 pre-signed URL → blob: download.
    #   page.route() works for fetch() requests; route.fetch() captures body.
    #
    # JPK_FA (Fakturownia Rails): page navigates /reports/jpk_fa.xml → 302 → S3 with
    #   Content-Disposition: attachment.  Playwright's CDP download interception fires
    #   before page.route() can see the request, so route.fetch() never runs.
    #
    # Unified approach: page.on("response") fires for BOTH patterns.  In the callback
    # we only store the S3 URL string (pure Python, no re-entrancy risk).  In the poll
    # loop we re-fetch the pre-signed URL via page.context.request.get() — safe because
    # it's called from outside any event callback.  Pre-signed S3 URLs are valid for
    # ~15 minutes, so there is plenty of time.
    captured_xml_urls: list[str] = []

    def _on_resp_capture(resp) -> None:
        # Pure Python only — NO Playwright API calls here.
        cd = resp.headers.get("content-disposition", "")
        ct = resp.headers.get("content-type", "")
        url = resp.url
        # Log every AWS hit so we can see what arrives in the browser
        if "amazonaws.com" in url:
            print(f"  🌐  [aws resp {resp.status}] {url[:90]!r}")
            print(f"       ct={ct!r}  cd={cd!r}")
        if "amazonaws.com" in url and "attachment" in cd.lower():
            print(f"  📡  [captured for re-fetch] {url[:80]!r}")
            captured_xml_urls.append(url)

    page.on("response", _on_resp_capture)

    sidebar_js = _build_sidebar_js(name, url, dest_name)

    def _inject_sb() -> None:
        """Re-inject sidebar if absent — safe to call from poll loop."""
        try:
            if not page.evaluate("!!document.getElementById('__zdv_sb')"):
                page.evaluate(sidebar_js)
        except Exception:
            pass

    _inject_sb()

    # --- Auto form-fill attempt (dispatched by report name) ---
    if name == "JPK_FA":
        _auto_fill_jpk_fa(page)
    elif name == "VAT Sales Register":
        _auto_fill_vat_register(page)
    else:
        _auto_fill_jpk_v7m(page)

    print(
        f"\n  🌐  Browser opened for {name}\n"
        f"     The form was pre-filled — waiting for download.\n"
        f"     If nothing happens, complete the steps manually.\n"
        f"     File will be saved to: {output_path}\n"
        f"     Click 'Skip ⏭' to skip and continue.\n"
    )
    logger.info(
        "%s: manual session started (timeout=%ds), waiting for %s",
        name,
        timeout_ms // 1000,
        output_path,
    )

    deadline = time.monotonic() + timeout_ms / 1000.0
    completed = False
    poll_n = 0
    while time.monotonic() < deadline:
        # page.wait_for_timeout yields to Playwright's event loop so
        # download events are processed and pending_downloads gets populated.
        page.wait_for_timeout(500)
        poll_n += 1
        _inject_sb()  # restore sidebar after any navigation
        if poll_n % 10 == 0:  # print every 5s so user knows we're alive
            print(
                f"  ⏳  Waiting... ({poll_n // 2}s elapsed, captured_urls={len(captured_xml_urls)})"
            )

        # Re-fetch any S3 pre-signed URLs captured by the response listener.
        # Called from the main poll loop (NOT inside an event callback) so
        # page.context.request.get() is safe — no re-entrancy risk.
        for s3_url in list(captured_xml_urls):
            captured_xml_urls.remove(s3_url)
            try:
                print(f"  🔄  Re-fetching S3 URL …{s3_url[-50:]!r}")
                api_resp = page.context.request.get(s3_url, timeout=30_000)
                if api_resp.ok:
                    body = api_resp.body()
                    if len(body) > 500:
                        output_path.write_bytes(body)
                        print(f"  📥  Saved via S3 re-fetch ({len(body)} bytes) → {output_path}")
                        logger.info(
                            "%s: S3 re-fetch saved to %s (%d bytes)", name, output_path, len(body)
                        )
                    else:
                        print(f"  ⚠️  S3 re-fetch body too small ({len(body)} bytes), skipping")
                else:
                    print(f"  ⚠️  S3 re-fetch returned HTTP {api_resp.status}")
            except Exception as exc:
                print(f"  ❌  S3 re-fetch failed: {exc}")
                logger.warning("%s: S3 re-fetch error: %s", name, exc)

        if output_path.exists() and output_path.stat().st_size >= 100:
            try:
                page.evaluate("window.__zdv_mark_done(0)")
            except Exception:
                pass
            completed = True
            logger.info("%s: file detected at %s — session complete", name, output_path)
            page.wait_for_timeout(2000)  # let user see the ✅
            break

        try:
            skipped = page.evaluate(
                "(function(){var m=document.cookie.match(/(?:^|; )__zdv_skipped_0=([^;]*)/);return m?decodeURIComponent(m[1])==='true':false;})()"
            )
        except Exception:
            skipped = False
        if skipped:
            logger.info("%s: skipped by user", name)
            break

    try:
        page.remove_listener("response", _on_resp_capture)
    except Exception:
        pass

    return completed


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


def _try_generate_and_download(
    page, output_path: Path, timeout_ms: int, *, button_texts: list[str] | None = None
) -> bool:
    """Click 'Generuj raport', wait for the async job result, then download it.

    Fakturownia report generation is asynchronous: clicking the submit button
    starts a background job.  The download link appears in one of two ways
    depending on the report type:

    * ``#job_download_link a[href*='/jobs/']`` — modal job link (e.g. VAT
      Sales Register), which is present but hidden inside a Bootstrap modal;
      we use the direct HTTP fetch fallback for this case.
    * A named export button (e.g. "Eksport do XML" for JPK_FA) that appears
      in the page body once the job completes.

    We therefore try both paths after clicking the generate button.
    """
    if _has_job_link(page):
        # Job link already present (e.g. submitted=true auto-fired the job).
        # The main wait_for_selector path in _download_one_report will handle it.
        return False

    # Most report pages use input[name='commit'] labelled "Generuj raport".
    for generate_selector in (
        "input[name='commit']",
        "button[type='submit']",
    ):
        if page.locator(generate_selector).count() == 0:
            continue
        try:
            page.click(generate_selector, force=True, timeout=5000)
        except Exception:
            continue

        # After clicking the generate button, poll until the job result appears.
        # Two result patterns exist depending on the report type:
        #   A) A hidden-modal job link: `#job_download_link a[href*='/jobs/']`
        #      (e.g. VAT Sales Register) — download via direct HTTP fetch.
        #   B) A named export button (e.g. "Eksport do XML" for JPK_FA) that
        #      appears in the page body once the backend job completes.
        # We poll for both simultaneously so neither blocks the other.
        combined_job_sel = (
            "#job_download_link a[href*='/jobs/'], a[href*='/jobs/'][href$='/result']"
        )
        deadline = time.monotonic() + timeout_ms / 1000.0
        while time.monotonic() < deadline:
            # Path A — job link (hidden modal)
            if page.locator(combined_job_sel).count() > 0:
                if _download_via_job_url(page, combined_job_sel, output_path, timeout_ms=30_000):
                    return True
                try:
                    with page.expect_download(timeout=30_000) as dl_info:
                        page.click(combined_job_sel, force=True, timeout=10_000)
                    dl_info.value.save_as(str(output_path))
                    return True
                except Exception:
                    pass

            # Path B — named export button appears after job completion
            if button_texts:
                for label in button_texts:
                    try:
                        if page.get_by_text(label, exact=False).count() == 0:
                            continue
                        with page.expect_download(timeout=30_000) as dl_info:
                            page.get_by_text(label, exact=False).first.click(
                                force=True, timeout=5_000
                            )
                        dl_info.value.save_as(str(output_path))
                        return True
                    except Exception:
                        continue

            page.wait_for_timeout(2_000)

        # Button was clicked but no result appeared — stop trying.
        return False
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
