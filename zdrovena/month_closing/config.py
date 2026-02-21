"""
zdrovena.month_closing.config – Month-close configuration
===========================================================
Company details, vendor definitions, Zoho / KSeF / Google Ads settings,
and keychain service names used during the monthly accounting close.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from zdrovena.common.config import (
    DEFAULT_DOMAIN as FAKTUROWNIA_DOMAIN,
    DEFAULT_RETRY_COUNT as API_RETRY_COUNT,
    DEFAULT_RETRY_DELAY as API_RETRY_DELAY,
    DEFAULT_TIMEOUT as API_TIMEOUT,
    DEFAULT_PDF_DELAY as PDF_DOWNLOAD_DELAY,
    KEYCHAIN_ACCOUNT,
)

# ─── Polish Month Names (filesystem-safe, no diacritics) ─────────────────────

POLISH_MONTHS: dict[int, str] = {
    1: "styczen", 2: "luty", 3: "marzec", 4: "kwiecien",
    5: "maj", 6: "czerwiec", 7: "lipiec", 8: "sierpien",
    9: "wrzesien", 10: "pazdziernik", 11: "listopad", 12: "grudzien",
}

ENGLISH_MONTHS: dict[int, str] = {
    1: "January", 2: "February", 3: "March", 4: "April",
    5: "May", 6: "June", 7: "July", 8: "August",
    9: "September", 10: "October", 11: "November", 12: "December",
}

# ─── Base Directory ───────────────────────────────────────────────────────────

BASE_DIR: Path = Path.home() / "Documents" / "Humio" / "faktury"

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
        "url": "https://zdrovena.fakturownia.pl/reports/jpk_fa",
    },
    {
        "name": "JPK_V7M",
        "glob": "zdrovena-*-jpkv7m*",
        "dest_name": "JPK_V7M.xml",
        "url": "https://zdrovena.fakturownia.pl/accounting/app/reports/jpk_vat",
    },
    {
        "name": "VAT Sales Register",
        "glob": "zdrovena-????-??-??_*",
        "dest_name": "Wykaz_sprzedazy_VAT.pdf",
        "url": "https://zdrovena.fakturownia.pl/reports/income_tax_records",
    },
]

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
    skip: bool = False


EXPECTED_VENDORS: list[VendorConfig] = [
    VendorConfig(name="Shopify",           pattern="shopify",    email="billing@shopify.com"),
    VendorConfig(name="BaseLinker",        pattern="baselinker", email="bok@baselinker.com",
                 link_re=r'https://panel[^"<>\s]+baselinker\.com/payment/printout\.php\?invoice=[^"<>\s]+'),
    VendorConfig(name="Allegro",           pattern="allegro",    email="allegro"),
    VendorConfig(name="PayU",              pattern="payu",       email="payu"),
    VendorConfig(name="InPost",            pattern="inpost",     email="inpost"),
    VendorConfig(name="Apaczka",           pattern="alsendo",    email="apaczka"),
    VendorConfig(name="Canva",             pattern="canva",      email="canva",
                 manual=True,
                 download_glob="invoice-?????-????????.pdf",
                 fallback_url="https://www.canva.com/invoices",
                 invoice_id_re=r"invoices(?:/|%2[Ff]|%252[Ff])(\d{5}-\d{8})",
                 invoice_file_tpl="invoice-{id}.pdf"),
    VendorConfig(name="Google Ads",        pattern="google",     email="payments-noreply",
                 manual=True,
                 download_glob="[0-9]?????????.pdf",
                 fallback_url="https://ads.google.com/aw/billing/documents?ocid=3849995102",
                 invoice_id_re=r"(?:Invoice|Faktura)[^0-9]*?(\d{10,})",
                 invoice_file_tpl="{id}.pdf"),
    VendorConfig(name="PulsePure",         pattern="pulsepure",  email="pulsepure"),
    VendorConfig(name="Accounting/Bożena", pattern="ogorzalek",  email="ogorzalek"),
]

# ─── Keychain Service Names (macOS Keyring) ──────────────────────────────────

KEYCHAIN_SERVICE_FAKTUROWNIA = "fakturownia_api_token"
KEYCHAIN_SERVICE_ZOHO_SMTP = "zoho_smtp_password"
KEYCHAIN_SERVICE_ZOHO_CLIENT_ID = "zoho_client_id"
KEYCHAIN_SERVICE_ZOHO_CLIENT_SECRET = "zoho_client_secret"
KEYCHAIN_SERVICE_ZOHO_REFRESH_TOKEN = "zoho_refresh_token"
KEYCHAIN_SERVICE_KSEF_CERT = "ksef_certificate"
KEYCHAIN_SERVICE_KSEF_KEY = "ksef_private_key"
KEYCHAIN_SERVICE_KSEF_KEY_PASS = "ksef_key_password"

KEYCHAIN_SERVICE_GADS_DEV_TOKEN = "gads_developer_token"
KEYCHAIN_SERVICE_GADS_CLIENT_ID = "gads_client_id"
KEYCHAIN_SERVICE_GADS_CLIENT_SECRET = "gads_client_secret"
KEYCHAIN_SERVICE_GADS_REFRESH_TOKEN = "gads_refresh_token"

# ─── Google Ads ───────────────────────────────────────────────────────────────

GOOGLE_ADS_ENABLED = True
GOOGLE_ADS_CUSTOMER_ID = "3849995102"
GOOGLE_ADS_LOGIN_CUSTOMER_ID: str | None = None

# ─── KSeF ─────────────────────────────────────────────────────────────────────

KSEF_ENABLED = True
KSEF_API_URL = "https://api.ksef.mf.gov.pl/v2"
KSEF_AUTH_POLL_INTERVAL = 2
KSEF_AUTH_POLL_MAX = 30

# ─── Cost Invoice Collection ─────────────────────────────────────────────────

COST_INVOICE_OVERLAP_DAYS = 20

# ─── Manual Invoice Download Watcher ──────────────────────────────────────

DOWNLOAD_WATCH_DIR = Path.home() / "Downloads"
DOWNLOAD_WATCH_TIMEOUT = 120
DOWNLOAD_WATCH_POLL = 2
