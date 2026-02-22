"""
zdrovena.common.config – Shared defaults
==========================================
Central configuration constants shared by all zdrovena modules.
"""

# ─── Fakturownia API ──────────────────────────────────────────────────────────

DEFAULT_DOMAIN: str = "zdrovena.fakturownia.pl"

# ─── macOS Keychain (via keyring) ─────────────────────────────────────────────

KEYCHAIN_ACCOUNT: str = "humio"

# Fakturownia
KEYCHAIN_SERVICE: str = "fakturownia_api_token"
KEYCHAIN_SERVICE_FAKTUROWNIA: str = "fakturownia_api_token"
KEYCHAIN_SERVICE_FAKTUROWNIA_LOGIN: str = "fakturownia_login"
KEYCHAIN_SERVICE_FAKTUROWNIA_PASSWORD: str = "fakturownia_password"

# Zoho
KEYCHAIN_SERVICE_ZOHO_SMTP: str = "zoho_smtp_password"
KEYCHAIN_SERVICE_ZOHO_CLIENT_ID: str = "zoho_client_id"
KEYCHAIN_SERVICE_ZOHO_CLIENT_SECRET: str = "zoho_client_secret"
KEYCHAIN_SERVICE_ZOHO_REFRESH_TOKEN: str = "zoho_refresh_token"

# KSeF
KEYCHAIN_SERVICE_KSEF_CERT: str = "ksef_certificate"
KEYCHAIN_SERVICE_KSEF_KEY: str = "ksef_private_key"
KEYCHAIN_SERVICE_KSEF_KEY_PASS: str = "ksef_key_password"

# Google Ads
KEYCHAIN_SERVICE_GADS_DEV_TOKEN: str = "gads_developer_token"
KEYCHAIN_SERVICE_GADS_CLIENT_ID: str = "gads_client_id"
KEYCHAIN_SERVICE_GADS_CLIENT_SECRET: str = "gads_client_secret"
KEYCHAIN_SERVICE_GADS_REFRESH_TOKEN: str = "gads_refresh_token"

# ─── HTTP / Retry defaults ───────────────────────────────────────────────────

DEFAULT_RETRY_COUNT: int = 3
DEFAULT_RETRY_DELAY: float = 2.0   # seconds; doubles on each retry
DEFAULT_TIMEOUT: int = 30          # seconds
DEFAULT_PER_PAGE: int = 100
DEFAULT_PDF_DELAY: float = 0.5     # delay between consecutive PDF downloads
