"""
zdrovena.audit.report_downloader – Fakturownia UI Report Downloader
=====================================================================
Downloads reports from Fakturownia's web UI using Playwright (headless
Chromium).  These reports are only available via the browser interface
and cannot be fetched through the REST API.

Flow
----
1. Login at ``/login`` (form POST).
2. Navigate to the report page with ``submitted=true`` — this
   auto-triggers a background job that generates the report.
3. Wait for the job to finish (report table renders on the page).
4. Render the page to PDF with ``page.pdf()`` using print media
   emulation so navigation chrome is hidden.

Requires::

    pip install playwright
    playwright install chromium
"""

from __future__ import annotations

import logging
from pathlib import Path

import keyring

from zdrovena.common.config import (
    KEYCHAIN_ACCOUNT,
    KEYCHAIN_SERVICE_FAKTUROWNIA_LOGIN,
    KEYCHAIN_SERVICE_FAKTUROWNIA_PASSWORD,
)

logger = logging.getLogger("zdrovena.audit.report")


class EmptyReportError(Exception):
    """Raised when the generated report contains no data."""


# ── Keychain services ────────────────────────────────────────────────────────

KEYCHAIN_LOGIN_SERVICE = KEYCHAIN_SERVICE_FAKTUROWNIA_LOGIN
KEYCHAIN_PASSWORD_SERVICE = KEYCHAIN_SERVICE_FAKTUROWNIA_PASSWORD

# ── URLs ─────────────────────────────────────────────────────────────────────

BASE_URL = "https://zdrovena.fakturownia.pl"
LOGIN_URL = f"{BASE_URL}/login"

# ── Available report kinds ───────────────────────────────────────────────────

REPORT_KINDS: dict[str, str] = {
    "vat-sales": "income_tax_records",
    "income": "invoice_list",
    "expenses": "expense_invoice_list",
    "unpaid": "unpaid_invoice_list",
    "products-sales": "products_income",
    "products-expense": "products_expense",
    "products-margin": "products_margin",
}


def _get_credentials() -> tuple[str, str]:
    """Read Fakturownia login/password from macOS Keychain."""
    login = keyring.get_password(KEYCHAIN_LOGIN_SERVICE, KEYCHAIN_ACCOUNT)
    password = keyring.get_password(KEYCHAIN_PASSWORD_SERVICE, KEYCHAIN_ACCOUNT)
    if not login or not password:
        raise RuntimeError(
            "Fakturownia login credentials not found in Keychain.\n"
            "Store them with:\n"
            f"  python3 -c \"import keyring; keyring.set_password('{KEYCHAIN_LOGIN_SERVICE}', "
            f"'{KEYCHAIN_ACCOUNT}', 'YOUR_EMAIL')\"\n"
            f"  python3 -c \"import keyring; keyring.set_password('{KEYCHAIN_PASSWORD_SERVICE}', "
            f"'{KEYCHAIN_ACCOUNT}', 'YOUR_PASSWORD')\""
        )
    return login, password


def download_report(
    date_from: str,
    date_to: str,
    output_path: Path,
    *,
    kind: str = "vat-sales",
    headless: bool = True,
    timeout: int = 120_000,
) -> Path:
    """
    Download a Fakturownia report as PDF via headless browser.

    Parameters
    ----------
    date_from, date_to : "YYYY-MM-DD"
    output_path : destination file path (will create parent dirs)
    kind : report type key from REPORT_KINDS
    headless : run browser without visible window
    timeout : max wait time in milliseconds for report generation

    Returns
    -------
    Path to the downloaded file.
    """
    try:
        from playwright.sync_api import sync_playwright
        from playwright_stealth import Stealth
    except ImportError as exc:
        raise RuntimeError(
            "Playwright and playwright-stealth are required for report downloads.\n"
            "Install with:  pip install playwright playwright-stealth && playwright install chromium"
        ) from exc

    report_kind = REPORT_KINDS.get(kind)
    if not report_kind:
        raise ValueError(
            f"Unknown report kind: {kind!r}. Available: {', '.join(REPORT_KINDS.keys())}"
        )

    login, password = _get_credentials()
    report_url = f"{BASE_URL}/reports/{report_kind}"

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    logger.info("Launching browser (headless=%s)...", headless)

    with Stealth().use_sync(sync_playwright()) as pw:
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        # Block the ConsentManager script — it creates an overlay and
        # keeps polling, preventing networkidle from ever firing.
        page.route("**/*consentmanager*", lambda route: route.abort())

        # ── Step 1: Login ────────────────────────────────────────────────
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
            page_content = page.content()
            if "Nie masz uprawnień do tego konta!" in page_content:
                raise RuntimeError(
                    f"Fakturownia login failed: The account '{login}' does not have access to {BASE_URL}"
                ) from e
            if "Login/Hasło nie są poprawne" in page_content:
                raise RuntimeError(
                    f"Fakturownia login failed: Invalid email or password for '{login}'"
                ) from e

            # Check for error messages on the page
            errors = page.query_selector_all(".alert, .error, .notice, .flash")
            error_texts = [err.inner_text().strip() for err in errors if err.inner_text().strip()]
            if error_texts:
                raise RuntimeError(f"Fakturownia login failed: {', '.join(error_texts)}") from e

            # Check if we got redirected to the main page without access
            if "#no_account=app" in page.url:
                raise RuntimeError(
                    f"Fakturownia login failed: The account '{login}' does not have access to {BASE_URL}"
                ) from e

            raise RuntimeError(f"Fakturownia login timed out. Current URL: {page.url}") from e
        logger.info("Login OK → %s", page.url)

        # ── Step 2: Load report page ─────────────────────────────────────
        # submitted=true auto-triggers the background report job.
        params = (
            f"?date_from={date_from}&date_to={date_to}"
            f"&submitted=true&currency_convert_to_main=false"
        )
        logger.info("Loading report page: %s", report_url)
        page.goto(report_url + params, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)

        # ── Step 3: Wait for the report job to finish ────────────────────
        # The page JS polls /jobs/worker.json.  When the job finishes
        # the report table is rendered and action links appear.
        # For empty reports (no invoices in period) the job finishes
        # but the table has only headers and no data rows.
        logger.info("Waiting for report job to finish (timeout=%ds)...", timeout // 1000)

        dl_link_sel = "#job_download_link a[href*='/jobs/']"
        try:
            page.wait_for_selector(dl_link_sel, state="attached", timeout=timeout)
        except Exception:
            # Timeout — check if the report rendered with no data
            pass

        # Verify the report actually contains data rows (not just headers)
        data_rows = page.query_selector_all("table tr td")
        if not data_rows:
            browser.close()
            raise EmptyReportError(f"Raport pusty — brak danych za okres {date_from} → {date_to}.")

        logger.info("Report job finished (%d data cells found).", len(data_rows))

        # ── Step 4: Render the report page to PDF ─────────────────────
        # The report table is already rendered on the page.  We hide
        # the navigation chrome via print media emulation and use
        # Playwright's page.pdf() to produce a clean PDF.
        logger.info("Rendering page to PDF...")

        # Hide navbar, sidebar, footer, buttons — keep only .content
        page.evaluate("""() => {
            const hide = (sel) => {
                document.querySelectorAll(sel).forEach(el => {
                    el.style.display = 'none';
                });
            };
            hide('nav, .navbar, .sidebar, footer, .footer');
            hide('#sugester-changelog-bubble, .sugester_widget');
            hide('.report-menu-button-more, .btn-glow');
            hide('#job_download_link, .progress-bar-container');

            // Also remove the "Wydruk" button row
            hide('input[type=submit]');
        }""")

        page.emulate_media(media="print")
        page.pdf(
            path=str(output_path),
            format="A4",
            landscape=True,
            print_background=True,
            margin={"top": "10mm", "right": "10mm", "bottom": "10mm", "left": "10mm"},
        )

        size = output_path.stat().st_size
        logger.info("PDF saved: %s (%d bytes)", output_path, size)

        browser.close()

    return output_path
