"""
POC: Batch invoice download via Zoho Mail + browser-use
========================================================
Searches Zoho Mail for Canva and Google Ads invoice emails,
extracts links/IDs, then uses browser-use to download PDFs
in a single browser session with user login.

Usage:
    .venv/bin/python poc_browser_batch.py --year 2026 --month 3
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import keyring

from zdrovena.month_closing.config import (
    EXPECTED_VENDORS,
    KEYCHAIN_ACCOUNT,
    KEYCHAIN_SERVICE_ZOHO_CLIENT_ID,
    KEYCHAIN_SERVICE_ZOHO_CLIENT_SECRET,
    KEYCHAIN_SERVICE_ZOHO_REFRESH_TOKEN,
    VendorConfig,
)
from zdrovena.month_closing.zoho_mail import ZohoMailClient

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
logger = logging.getLogger("poc_browser_batch")

# Vendors we want to handle in this POC
POC_VENDORS = ("Canva", "Google Ads")


@dataclass
class PendingDownload:
    vendor: str
    url: str
    filename: str


def _get_secret(service: str) -> str:
    import os

    # Try env var first (service name uppercased, dots→underscores)
    env_key = service.upper().replace(".", "_").replace("-", "_")
    val = os.environ.get(env_key)
    if val:
        return val
    # Fallback to keyring
    try:
        val = keyring.get_password(service, KEYCHAIN_ACCOUNT)
    except Exception:
        val = None
    if not val:
        sys.exit(
            f"Missing secret: {service}\n"
            f"Set env var {env_key} or unlock keyring."
        )
    return val


def _date_range(year: int, month: int) -> tuple[str, str]:
    from calendar import monthrange

    _, last_day = monthrange(year, month)
    return f"{year}/{month:02d}/01", f"{year}/{month:02d}/{last_day:02d}"


def collect_pending_downloads(year: int, month: int, save_dir: Path) -> list[PendingDownload]:
    """Use Zoho Mail to find invoice emails and extract download targets."""
    zoho = ZohoMailClient(
        client_id=_get_secret(KEYCHAIN_SERVICE_ZOHO_CLIENT_ID),
        client_secret=_get_secret(KEYCHAIN_SERVICE_ZOHO_CLIENT_SECRET),
        refresh_token=_get_secret(KEYCHAIN_SERVICE_ZOHO_REFRESH_TOKEN),
    )
    zoho.authenticate()
    logger.info("Zoho Mail authenticated")

    date_from, date_to = _date_range(year, month)
    pending: list[PendingDownload] = []

    for vendor_cfg in EXPECTED_VENDORS:
        if vendor_cfg.name not in POC_VENDORS:
            continue

        email_pattern = vendor_cfg.email or vendor_cfg.pattern
        logger.info("Searching Zoho for %s emails (%s)...", vendor_cfg.name, email_pattern)

        if vendor_cfg.name == "Canva" and vendor_cfg.invoice_id_re:
            # Canva: extract invoice IDs from email, build download URLs
            invoice_ids = zoho.extract_invoice_ids(
                search_term=email_pattern,
                date_from=date_from,
                date_to=date_to,
                invoice_id_re=vendor_cfg.invoice_id_re,
            )
            for inv in invoice_ids:
                inv_id = inv["id"]
                tpl = vendor_cfg.invoice_file_tpl or "invoice-{id}.pdf"
                filename = tpl.format(id=inv_id)
                dest = save_dir / filename
                if dest.exists():
                    logger.info("  Skipping %s — already exists", filename)
                    continue
                url = inv.get("url") or f"https://www.canva.com/invoices/{inv_id}"
                pending.append(PendingDownload(vendor="Canva", url=url, filename=filename))

        elif vendor_cfg.name == "Google Ads":
            # Google Ads: extract invoice URLs from email content
            result = zoho.search_and_download_vendor(
                vendor_name=vendor_cfg.name,
                search_term=email_pattern,
                date_from=date_from,
                date_to=date_to,
                save_dir=save_dir,
                manual=True,
                invoice_url_re=vendor_cfg.invoice_id_re,
            )
            urls = result.get("urls", [])
            for i, url in enumerate(urls):
                filename = f"GoogleAds_{year}{month:02d}_{i + 1}.pdf"
                dest = save_dir / filename
                if dest.exists():
                    logger.info("  Skipping %s — already exists", filename)
                    continue
                pending.append(PendingDownload(vendor="Google Ads", url=url, filename=filename))

    return pending


async def download_batch(pending: list[PendingDownload], save_dir: Path) -> None:
    """Open browser-use and download all pending invoices in one session."""
    from browser_use import Agent, BrowserProfile
    from browser_use.browser.session import BrowserSession

    save_dir.mkdir(parents=True, exist_ok=True)

    profile = BrowserProfile(
        headless=True,
        accept_downloads=True,
        downloads_path=str(save_dir),
    )
    browser = BrowserSession(browser_profile=profile)

    # Build task description for the AI agent
    task_lines = [
        "You need to download invoice PDFs. The user may need to log in manually — "
        "wait for them if a login page appears. For each URL below, navigate to it "
        "and download the PDF invoice.\n"
    ]
    for i, item in enumerate(pending, 1):
        task_lines.append(
            f"{i}. [{item.vendor}] Go to: {item.url} — save as '{item.filename}'"
        )

    task_lines.append(
        "\nAfter visiting each URL, click the download button if the PDF doesn't "
        "auto-download. Wait for each download to complete before moving to the next."
    )

    task = "\n".join(task_lines)
    logger.info("Browser task:\n%s", task)

    print("\n" + "=" * 60)
    print("  BROWSER SESSION STARTING")
    print("  A browser window will open. Log in when prompted.")
    print("  browser-use will handle navigation and downloads.")
    print("=" * 60 + "\n")

    from browser_use.llm.openai.chat import ChatOpenAI

    llm = ChatOpenAI(model="gpt-4.1-nano")

    agent = Agent(
        task=task,
        llm=llm,
        browser=browser,
        use_vision=True,
        max_failures=3,
        max_actions_per_step=3,
    )

    try:
        result = await agent.run()
        logger.info("Agent finished. Result: %s", result.is_done() if result else "unknown")
    finally:
        await browser.stop()

    # Report what was downloaded
    print("\n" + "=" * 60)
    print("  DOWNLOAD SUMMARY")
    print("=" * 60)
    for item in pending:
        dest = save_dir / item.filename
        if dest.exists():
            print(f"  OK  {item.vendor}: {item.filename} ({dest.stat().st_size} bytes)")
        else:
            print(f"  MISSING  {item.vendor}: {item.filename}")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="POC: batch invoice download")
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--month", type=int, required=True)
    parser.add_argument("--save-dir", type=Path, default=None)
    args = parser.parse_args()

    save_dir = args.save_dir or Path.home() / ".zdrovena" / "poc_downloads" / f"{args.year}_{args.month:02d}"

    logger.info("Collecting pending downloads for %d/%02d...", args.year, args.month)
    pending = collect_pending_downloads(args.year, args.month, save_dir)

    if not pending:
        print("Nothing to download — all invoices already present or no emails found.")
        return

    print(f"\nFound {len(pending)} invoice(s) to download:")
    for item in pending:
        print(f"  - {item.vendor}: {item.url[:80]}...")

    asyncio.run(download_batch(pending, save_dir))


if __name__ == "__main__":
    main()
