"""
zdrovena.month_closing.canva_downloader – Canva PDF Downloader
================================================================
Downloads Canva invoices using Playwright with a persistent profile.
Uses the saved session to navigate to /invoices/{id} and click
the "Download invoice" button.
"""

import glob
import logging
import os
import time
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
    from playwright_stealth import Stealth

    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False

logger = logging.getLogger("zdrovena.canva")

PROFILE_DIR = Path.home() / ".zdrovena" / "canva_profile"
CANVA_INVOICES_URL = "https://www.canva.com/invoices"


def get_chrome_executable_path() -> str | None:
    import sys

    if sys.platform == "darwin":
        paths = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ]
        for p in paths:
            if os.path.exists(p):
                return p
    return None


def _clean_singleton_locks() -> None:
    """Remove stale Chrome singleton lock files from the profile directory."""
    for f in glob.glob(str(PROFILE_DIR / "Singleton*")):
        try:
            os.remove(f)
        except OSError:
            pass


def _launch_context(pw, *, headless: bool, accept_downloads: bool = False):
    """Launch a persistent Chromium context with stealth settings."""
    executable_path = get_chrome_executable_path()
    return pw.chromium.launch_persistent_context(
        user_data_dir=PROFILE_DIR,
        headless=headless,
        executable_path=executable_path,
        args=["--disable-blink-features=AutomationControlled"],
        accept_downloads=accept_downloads,
        viewport={"width": 1280, "height": 800},
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
    )


def _interactive_login(pw, start_url: str = "https://www.canva.com/login") -> None:
    """Open a visible browser for the user to log in, then wait for them to close it."""
    _clean_singleton_locks()
    context = _launch_context(pw, headless=False)
    page = context.new_page()
    page.goto(start_url, timeout=30_000)

    print("\n╔══════════════════════════════════════════════════════╗")
    print("║  Canva session expired — manual login required.     ║")
    print("║                                                     ║")
    print("║  1. Log in to Canva in the opened browser window.   ║")
    print("║  2. Make sure you are on the TEAM account.          ║")
    print("║  3. Close the browser window when done.             ║")
    print("╚══════════════════════════════════════════════════════╝\n")
    logger.info("Waiting for user to complete Canva login…")

    try:
        while len(context.pages) > 0:
            time.sleep(1)
    except Exception:
        pass

    logger.info("Browser closed. Session refreshed.")


def setup_canva_login() -> None:
    """
    Opens a non-headless browser to allow the user to log in manually.
    Saves the session state to PROFILE_DIR.
    """
    if not _PLAYWRIGHT_AVAILABLE:
        raise RuntimeError("Playwright is required: pip install 'zdrovena[report]'")
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    _clean_singleton_locks()

    print("Opening Canva in a browser. Please log in manually.")
    print(f"Profile will be saved to: {PROFILE_DIR}")

    with Stealth().use_sync(sync_playwright()) as pw:
        _interactive_login(pw)

    print("Session saved successfully!")


def _try_download(pw, invoice_url: str, output_path: Path, headless: bool) -> bool:
    """Attempt to download an invoice. Returns True on success, False if session expired."""
    _clean_singleton_locks()
    context = _launch_context(pw, headless=headless, accept_downloads=True)
    try:
        page = context.new_page()
        with page.expect_download(timeout=30_000) as dl_info:
            page.goto(invoice_url, timeout=30_000)
        download = dl_info.value
        download.save_as(output_path)
        return True
    except Exception:
        return False
    finally:
        context.close()


def download_canva_invoice(invoice_id: str, output_path: Path, headless: bool = True) -> Path:
    """
    Downloads a single Canva invoice PDF by navigating to its page.
    The invoice page auto-triggers a PDF download on load.

    If the download fails (session expired), a visible browser window
    is opened on the invoice URL so the user can log in. After the
    user closes the browser, the download is retried headlessly.

    Args:
        invoice_id: The Canva invoice ID (e.g. '04785-37192234').
        output_path: Where to save the PDF.
        headless: Whether to run browser headless (default True).

    Returns:
        The path to the saved PDF.
    """
    if not _PLAYWRIGHT_AVAILABLE:
        raise RuntimeError("Playwright is required: pip install 'zdrovena[report]'")

    if not PROFILE_DIR.exists():
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    invoice_url = f"{CANVA_INVOICES_URL}/{invoice_id}"
    logger.info("Downloading Canva invoice %s → %s", invoice_id, output_path)

    with Stealth().use_sync(sync_playwright()) as pw:
        # First attempt — headless
        if _try_download(pw, invoice_url, output_path, headless=headless):
            size = output_path.stat().st_size
            if size >= 100:
                logger.info("Canva invoice %s saved (%d bytes)", invoice_id, size)
                return output_path

        # Download failed → session likely expired, open browser for re-login
        logger.warning("Canva download failed, launching browser for re-login")
        _interactive_login(pw, start_url=invoice_url)

        # Retry after re-login
        if not _try_download(pw, invoice_url, output_path, headless=True):
            raise RuntimeError(
                f"Failed to download Canva invoice {invoice_id} even after re-login."
            )

        size = output_path.stat().st_size
        if size < 100:
            raise RuntimeError(f"Downloaded file too small ({size} bytes), likely not a valid PDF.")
        logger.info("Canva invoice %s saved (%d bytes)", invoice_id, size)

    return output_path
