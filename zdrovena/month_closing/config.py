"""
zdrovena.month_closing.config – Month-close configuration
===========================================================
Company details, vendor definitions, Zoho / KSeF / Google Ads settings,
and keychain service names used during the monthly accounting close.
"""
# ruff: noqa: F401  — re-exported constants are intentionally imported here

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from zdrovena.common.config import (
    DEFAULT_DOMAIN as FAKTUROWNIA_DOMAIN,
)
from zdrovena.common.config import (
    DEFAULT_PDF_DELAY as PDF_DOWNLOAD_DELAY,
)
from zdrovena.common.config import (
    DEFAULT_RETRY_COUNT as API_RETRY_COUNT,
)
from zdrovena.common.config import (
    DEFAULT_RETRY_DELAY as API_RETRY_DELAY,
)
from zdrovena.common.config import (
    DEFAULT_TIMEOUT as API_TIMEOUT,
)
from zdrovena.common.config import (
    KEYCHAIN_ACCOUNT,
    KEYCHAIN_SERVICE_FAKTUROWNIA,
    KEYCHAIN_SERVICE_FAKTUROWNIA_LOGIN,
    KEYCHAIN_SERVICE_FAKTUROWNIA_PASSWORD,
    KEYCHAIN_SERVICE_GADS_CLIENT_ID,
    KEYCHAIN_SERVICE_GADS_CLIENT_SECRET,
    KEYCHAIN_SERVICE_GADS_DEV_TOKEN,
    KEYCHAIN_SERVICE_GADS_REFRESH_TOKEN,
    KEYCHAIN_SERVICE_KSEF_CERT,
    KEYCHAIN_SERVICE_KSEF_KEY,
    KEYCHAIN_SERVICE_KSEF_KEY_PASS,
    KEYCHAIN_SERVICE_ZOHO_CLIENT_ID,
    KEYCHAIN_SERVICE_ZOHO_CLIENT_SECRET,
    KEYCHAIN_SERVICE_ZOHO_REFRESH_TOKEN,
    KEYCHAIN_SERVICE_ZOHO_SMTP,
)
from zdrovena.common.formatting import (
    ENGLISH_MONTHS,
)
from zdrovena.common.formatting import (
    MONTHS_FULL as POLISH_MONTHS,
)

# ─── Base Directory ───────────────────────────────────────────────────────────

BASE_DIR: Path = Path(
    os.environ.get("FAKTUROWNIA_BASE_DIR", str(Path.home() / "Documents" / "Humio" / "faktury"))
)

# ─── Company Details (used in JPK XML generation) ────────────────────────────

COMPANY_FULL_NAME = "Maria Gryzło ZDROVENA"
COMPANY_NIP = "7341123931"
COMPANY_BRAND = "ZDROVENA"
COMPANY_ADDRESS_STREET = "ul. Cieszynska6/12"
COMPANY_ADDRESS_CITY = "Kraków"
COMPANY_ADDRESS_ZIP = "30-015"
COMPANY_ADDRESS_COUNTRY = "PL"

# ─── Expected Reports (manual download from Fakturownia) ─────────────────────

FAKTUROWNIA_REPORTS: list[dict] = [
    {
        "name": "JPK_FA",
        "glob": "zdrovena-*-jpk_fa*",
        "dest_name": "JPK_FA.xml",
        # The server requires kind, query_date_kind and form_variant to start the
        # async generation job.  Without them, submitted=true is ignored and no job
        # is triggered.  date_from/date_to/submitted=true are appended at runtime.
        "url": "https://zdrovena.fakturownia.pl/reports/jpk_fa?kind=jpk_fa&query_date_kind=transaction_date&form_variant=4",
        "download_button_texts": ["Eksport do XML", "Export do XML"],
        "use_wizard_navigation": True,
    },
    {
        "name": "JPK_V7M",
        "glob": "zdrovena*v7*",
        "dest_name": "JPK_V7M.xml",
        "url": "https://zdrovena.fakturownia.pl/accounting/app/reports/jpk_vat",
        "append_date_params": False,
        "download_button_texts": ["Pobierz XML", "zapisz i generuj xml"],
        "use_wizard_navigation": True,
    },
    {
        "name": "VAT Sales Register",
        "glob": "zdrovena-????-??-??_*",
        "dest_name": "Wykaz_sprzedazy_VAT.pdf",
        # submitted=true starts the async job; the page then auto-navigates to S3.
        # use_wizard_navigation routes through _run_manual_browser_session which
        # captures the S3 PDF URL via page.on("response") and re-fetches it.
        "url": "https://zdrovena.fakturownia.pl/reports/income_tax_records",
        "use_wizard_navigation": True,
    },
]

# Runtime config for report automation. Keep Playwright as default now, but
# this seam allows a future browser-use/cloud adapter without changing preflight.
FAKTUROWNIA_REPORT_RUNTIME = "playwright"
FAKTUROWNIA_REPORT_TIMEOUT_MS = 120_000
FAKTUROWNIA_REPORT_DOWNLOAD_SELECTOR = "#job_download_link a[href*='/jobs/']"

# ─── Zoho Mail ────────────────────────────────────────────────────────────────

ZOHO_EMAIL = "piotr@wodahumio.pl"
ZOHO_SMTP_HOST = "smtp.zoho.eu"
ZOHO_SMTP_PORT = 465

ZOHO_MAIL_API_URL = "https://mail.zoho.eu/api"
ZOHO_ACCOUNTS_URL = "https://accounts.zoho.eu/oauth/v2/token"

# ─── Accountant ───────────────────────────────────────────────────────────────

ACCOUNTANT_EMAIL = "piotr@wodahumio.pl"

# ─── Expected Cost Invoice Vendors ───────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class VendorConfig:
    """Typed, immutable configuration for a single cost-invoice vendor."""

    name: str
    pattern: str
    email: str | None = None
    manual: bool = False
    link_re: str | None = None
    download_glob: str | None = None
    fallback_url: str | None = None
    invoice_id_re: str | None = None
    invoice_file_tpl: str | None = None
    browser_download: bool = False
    skip: bool = False


EXPECTED_VENDORS: list[VendorConfig] = [
    VendorConfig(name="Shopify", pattern="shopify", email="billing@shopify.com"),
    VendorConfig(name="Allegro", pattern="allegro", email="allegro"),
    VendorConfig(name="PayU", pattern="payu", email="payu"),
    VendorConfig(name="InPost", pattern="inpost", email="inpost"),
    VendorConfig(name="Apaczka", pattern="alsendo", email="apaczka"),
    VendorConfig(
        name="Canva",
        pattern="canva",
        email="canva",
        browser_download=True,
        download_glob="invoice-?????-????????.pdf",
        fallback_url="https://www.canva.com/invoices",
        invoice_id_re=r"invoices(?:/|%2[Ff]|%252[Ff])(\d{5}-\d{8})",
        invoice_file_tpl="invoice-{id}.pdf",
    ),
    VendorConfig(
        name="Google Ads",
        pattern="google",
        email="payments-noreply",
        manual=True,
        download_glob="[0-9]?????????.pdf",
        fallback_url="https://ads.google.com/aw/billing/documents?ocid=3849995102",
        invoice_id_re=r"(?:Invoice|Faktura)[^0-9]*?(\d{10,})",
        invoice_file_tpl="{id}.pdf",
    ),
    VendorConfig(name="PulsePure", pattern="pulsepure", email="pulsepure"),
    VendorConfig(name="Accounting/Bożena", pattern="ogorzalek", email="ogorzalek"),
]

# ─── Google Ads ───────────────────────────────────────────────────────────────

GOOGLE_ADS_ENABLED = True
GOOGLE_ADS_CUSTOMER_ID = "3849995102"
GOOGLE_ADS_LOGIN_CUSTOMER_ID: str | None = None

# ─── KSeF ─────────────────────────────────────────────────────────────────────

KSEF_ENABLED = os.environ.get("KSEF_ENABLED", "true").lower() not in ("false", "0", "no")
KSEF_API_URL = "https://api.ksef.mf.gov.pl/v2"
KSEF_AUTH_POLL_INTERVAL = 2
KSEF_AUTH_POLL_MAX = 30

# ─── Cost Invoice Collection ─────────────────────────────────────────────────

COST_INVOICE_OVERLAP_DAYS = 20

# ─── Manual Invoice Download Watcher ──────────────────────────────────────

DOWNLOAD_WATCH_DIR = BASE_DIR / "inbox"
DOWNLOAD_WATCH_TIMEOUT = 120
DOWNLOAD_WATCH_POLL = 2
