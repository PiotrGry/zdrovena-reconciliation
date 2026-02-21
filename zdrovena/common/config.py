"""
zdrovena.common.config – Shared defaults
==========================================
Central configuration constants shared by all zdrovena modules.
"""

# ─── Fakturownia API ──────────────────────────────────────────────────────────

DEFAULT_DOMAIN: str = "zdrovena.fakturownia.pl"

# ─── macOS Keychain (via keyring) ─────────────────────────────────────────────

KEYCHAIN_SERVICE: str = "fakturownia_api_token"
KEYCHAIN_ACCOUNT: str = "humio"

# ─── HTTP / Retry defaults ───────────────────────────────────────────────────

DEFAULT_RETRY_COUNT: int = 3
DEFAULT_RETRY_DELAY: float = 2.0   # seconds; doubles on each retry
DEFAULT_TIMEOUT: int = 30          # seconds
DEFAULT_PER_PAGE: int = 100
DEFAULT_PDF_DELAY: float = 0.5     # delay between consecutive PDF downloads
