"""
zdrovena.month_closing.commands.setup_cmd – Keychain & OAuth setup
====================================================================
Interactive wizards for storing credentials in macOS Keychain:

    zdrovena setup                 # full interactive wizard
    zdrovena setup --check         # verify all secrets exist
    zdrovena setup zoho            # Zoho Mail OAuth flow
    zdrovena setup gads            # Google Ads OAuth flow
"""

from __future__ import annotations

import argparse
import base64
import getpass
import sys
import webbrowser
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

try:
    import keyring
except ImportError:
    keyring = None  # type: ignore[assignment]

try:
    import requests
except ImportError:
    requests = None  # type: ignore[assignment]

from zdrovena.month_closing.config import (
    GOOGLE_ADS_ENABLED,
    KEYCHAIN_ACCOUNT,
    KEYCHAIN_SERVICE_FAKTUROWNIA,
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
    KSEF_ENABLED,
    ZOHO_ACCOUNTS_URL,
)

# ─── Secret definitions ──────────────────────────────────────────────────────

REQUIRED_SECRETS: list[dict[str, str]] = [
    {
        "service": KEYCHAIN_SERVICE_FAKTUROWNIA,
        "label": "Fakturownia API Token",
        "hint": "Find it at: https://zdrovena.fakturownia.pl → Settings → API",
    },
    {
        "service": KEYCHAIN_SERVICE_ZOHO_SMTP,
        "label": "Zoho SMTP Password",
        "hint": "Your Zoho email password (the one that worked in SMTP test)",
    },
]

ZOHO_OAUTH_SECRETS: list[dict[str, str]] = [
    {
        "service": KEYCHAIN_SERVICE_ZOHO_CLIENT_ID,
        "label": "Zoho OAuth Client ID",
        "hint": "From https://api-console.zoho.eu/ → Self Client",
    },
    {
        "service": KEYCHAIN_SERVICE_ZOHO_CLIENT_SECRET,
        "label": "Zoho OAuth Client Secret",
        "hint": "From https://api-console.zoho.eu/ → Self Client",
    },
    {
        "service": KEYCHAIN_SERVICE_ZOHO_REFRESH_TOKEN,
        "label": "Zoho OAuth Refresh Token",
        "hint": "Run: zdrovena setup zoho  for guided setup",
    },
]

KSEF_CERT_SECRETS: list[dict[str, str]] = [
    {"service": KEYCHAIN_SERVICE_KSEF_CERT, "label": "KSeF Certificate (.crt)"},
    {"service": KEYCHAIN_SERVICE_KSEF_KEY, "label": "KSeF Private Key (.key)"},
]

GOOGLE_ADS_SECRETS: list[dict[str, str]] = [
    {
        "service": KEYCHAIN_SERVICE_GADS_DEV_TOKEN,
        "label": "Google Ads Developer Token",
        "hint": "Google Ads → Tools & Settings → API Center",
    },
    {
        "service": KEYCHAIN_SERVICE_GADS_CLIENT_ID,
        "label": "Google Ads OAuth Client ID",
        "hint": "Google Cloud Console → APIs & Services → Credentials (Desktop app)",
    },
    {
        "service": KEYCHAIN_SERVICE_GADS_CLIENT_SECRET,
        "label": "Google Ads OAuth Client Secret",
        "hint": "Same page as Client ID above",
    },
    {
        "service": KEYCHAIN_SERVICE_GADS_REFRESH_TOKEN,
        "label": "Google Ads OAuth Refresh Token",
        "hint": "Run: zdrovena setup gads  for guided setup",
    },
]


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _ensure_keyring() -> None:
    if keyring is None:
        sys.exit("❌  'keyring' package is required.  Install with:  pip install keyring")


def _ensure_requests() -> None:
    if requests is None:
        sys.exit("❌  'requests' package is required.  Install with:  pip install requests")


def _secret_exists(service: str) -> bool:
    """Return True if a secret is already stored in the Keychain."""
    try:
        val = keyring.get_password(service, KEYCHAIN_ACCOUNT)
        return val is not None and len(val) > 0
    except Exception:
        return False


def _store_secret(service: str, label: str, hint: str = "") -> None:
    """Prompt the user and store the value in Keychain."""
    print(f"\n🔑  {label}")
    if hint:
        print(f"    ℹ️  {hint}")

    existing = _secret_exists(service)
    if existing:
        overwrite = input("    ⚠️  Value already stored. Overwrite? [y/N]: ").strip().lower()
        if overwrite != "y":
            print("    ⏭  Skipped.")
            return

    value = getpass.getpass("    Enter value (hidden): ").strip()
    if not value:
        print("    ⚠️  Empty value – skipped.")
        return

    keyring.set_password(service, KEYCHAIN_ACCOUNT, value)
    print(f"    ✅  Stored in Keychain (service={service!r}).")


def _import_file_to_keychain(service: str, label: str) -> None:
    """Read a PEM file from disk, base64-encode it, and store in Keychain."""
    print(f"\n🔐  {label}")

    existing = _secret_exists(service)
    if existing:
        overwrite = input("    ⚠️  Already stored in Keychain. Overwrite? [y/N]: ").strip().lower()
        if overwrite != "y":
            print("    ⏭  Skipped.")
            return

    file_path_str = input("    Enter full path to file (e.g. ~/fakturownia.crt): ").strip()
    if not file_path_str:
        print("    ⚠️  No path provided – skipped.")
        return

    file_path = Path(file_path_str).expanduser().resolve()
    if not file_path.exists():
        print(f"    ❌  File not found: {file_path}")
        return
    if not file_path.is_file():
        print(f"    ❌  Not a file: {file_path}")
        return

    raw_bytes = file_path.read_bytes()
    encoded = base64.b64encode(raw_bytes).decode("ascii")

    keyring.set_password(service, KEYCHAIN_ACCOUNT, encoded)
    print(f"    ✅  {file_path.name} ({len(raw_bytes)} bytes) → Keychain (service={service!r})")


# ─── Interactive wizard ──────────────────────────────────────────────────────


def setup_interactive() -> None:
    """Run the interactive setup wizard."""
    _ensure_keyring()

    print("=" * 60)
    print("  ZDROVENA – Keychain Secrets Setup")
    print("=" * 60)
    print("\nThis wizard stores credentials in your macOS Keychain.")
    print("They will never appear in plain-text files.\n")

    # Required secrets
    print("─── Required Secrets ───")
    for secret in REQUIRED_SECRETS:
        _store_secret(**secret)

    # Zoho OAuth
    print("\n─── Zoho Mail API (OAuth) ───")
    print("    💡 Tip: run  zdrovena setup zoho  for guided OAuth setup.")
    setup_oauth = input("    Store Zoho OAuth tokens manually here? [y/N]: ").strip().lower()
    if setup_oauth == "y":
        for secret in ZOHO_OAUTH_SECRETS:
            _store_secret(**secret)
    else:
        print("    ⏭  Skipped. Use:  zdrovena setup zoho")

    # Google Ads
    print("\n─── Google Ads API (OAuth) ───")
    if GOOGLE_ADS_ENABLED:
        print("    💡 Tip: run  zdrovena setup gads  for guided setup.")
        setup_gads = input("    Store Google Ads credentials manually here? [y/N]: ").strip().lower()
        if setup_gads == "y":
            for secret in GOOGLE_ADS_SECRETS:
                _store_secret(**secret)
        else:
            print("    ⏭  Skipped. Use:  zdrovena setup gads")
    else:
        print("    Google Ads is disabled – skipping.")

    # KSeF certificate import
    print("\n─── KSeF Certificate (Optional) ───")
    if KSEF_ENABLED:
        print("    KSeF is enabled. Import your .crt and .key files into Keychain.")
        setup_ksef = input("    Import KSeF certificate files now? [Y/n]: ").strip().lower()
        if setup_ksef != "n":
            for cert_entry in KSEF_CERT_SECRETS:
                _import_file_to_keychain(**cert_entry)
            _store_secret(
                service=KEYCHAIN_SERVICE_KSEF_KEY_PASS,
                label="KSeF Private Key Password",
                hint="The passphrase that protects your .key file (leave empty if unencrypted)",
            )
    else:
        print("    KSeF is disabled – skipping.")
        setup_ksef = input("    Import KSeF certificate files anyway? [y/N]: ").strip().lower()
        if setup_ksef == "y":
            for cert_entry in KSEF_CERT_SECRETS:
                _import_file_to_keychain(**cert_entry)
            _store_secret(
                service=KEYCHAIN_SERVICE_KSEF_KEY_PASS,
                label="KSeF Private Key Password",
                hint="The passphrase that protects your .key file (leave empty if unencrypted)",
            )

    print("\n✅  Setup complete.\n")


# ─── Check ────────────────────────────────────────────────────────────────────


def check_secrets() -> bool:
    """Verify that all required secrets are stored."""
    _ensure_keyring()

    print("🔍  Checking Keychain secrets …\n")
    all_ok = True

    for secret in REQUIRED_SECRETS:
        exists = _secret_exists(secret["service"])
        status = "✅" if exists else "❌  MISSING"
        print(f"  {status}  {secret['label']}  (service={secret['service']!r})")
        if not exists:
            all_ok = False

    print()
    print("  ─── Zoho OAuth ───")
    for secret in ZOHO_OAUTH_SECRETS:
        exists = _secret_exists(secret["service"])
        status = "✅" if exists else "❌  MISSING"
        print(f"  {status}  {secret['label']}  (service={secret['service']!r})")
        if not exists:
            all_ok = False

    print()
    print("  ─── Google Ads ───")
    for secret in GOOGLE_ADS_SECRETS:
        exists = _secret_exists(secret["service"])
        status = "✅" if exists else "⚠️  not set (optional)"
        print(f"  {status}  {secret['label']}  (service={secret['service']!r})")

    print()
    print("  ─── KSeF Certificate ───")
    for cert_entry in KSEF_CERT_SECRETS:
        exists = _secret_exists(cert_entry["service"])
        status = "✅" if exists else "⚠️  not set (optional)"
        print(f"  {status}  {cert_entry['label']}  (service={cert_entry['service']!r})")
    kp_exists = _secret_exists(KEYCHAIN_SERVICE_KSEF_KEY_PASS)
    kp_status = "✅" if kp_exists else "⚠️  not set (optional)"
    print(f"  {kp_status}  KSeF Key Password  (service={KEYCHAIN_SERVICE_KSEF_KEY_PASS!r})")

    print()
    if all_ok:
        print("✅  All required secrets are present.")
    else:
        print("❌  Some required secrets are missing.")
        print("    Run:  zdrovena setup")
        print("    For Zoho OAuth:  zdrovena setup zoho")
    return all_ok


# ─── Zoho OAuth flow ─────────────────────────────────────────────────────────


def setup_zoho() -> None:
    """Guided Zoho Mail OAuth token setup."""
    _ensure_keyring()
    _ensure_requests()

    print("=" * 60)
    print("  ZDROVENA – Zoho Mail OAuth Setup")
    print("=" * 60)
    print()
    print("Before running this, complete these steps:")
    print()
    print("  1. Go to  https://api-console.zoho.eu/")
    print('  2. "Add Client" → "Self Client"')
    print("  3. Copy Client ID and Client Secret")
    print('  4. "Generate Code" tab → enter scope:')
    print("     ZohoMail.messages.READ,ZohoMail.attachments.READ,"
          "ZohoMail.accounts.READ,ZohoMail.folders.READ")
    print("  5. Time duration: 10 minutes → Create")
    print("  6. Copy the generated code")
    print()

    client_id = input("Client ID: ").strip()
    if not client_id:
        sys.exit("❌ Client ID is required.")

    client_secret = input("Client Secret: ").strip()
    if not client_secret:
        sys.exit("❌ Client Secret is required.")

    grant_code = input("Grant Code: ").strip()
    if not grant_code:
        sys.exit("❌ Grant Code is required.")

    # Exchange grant code for tokens
    print("\n⏳ Exchanging grant code for tokens …")
    resp = requests.post(
        ZOHO_ACCOUNTS_URL,
        params={
            "code": grant_code,
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "authorization_code",
        },
        timeout=15,
    )

    data = resp.json()

    if "error" in data:
        print(f"\n❌ Zoho returned an error: {data['error']}")
        if "error_description" in data:
            print(f"   {data['error_description']}")
        sys.exit(1)

    access_token = data.get("access_token")
    refresh_token = data.get("refresh_token")

    if not access_token:
        print(f"\n❌ No access_token in response: {data}")
        sys.exit(1)

    if not refresh_token:
        print(f"\n⚠️  No refresh_token in response (may already exist): {data}")
        print("   If you already have a refresh_token stored, it's still valid.")
        print("   Otherwise, revoke tokens at https://api-console.zoho.eu/ and retry.")
        sys.exit(1)

    # Store in Keychain
    print("\n💾 Storing credentials in macOS Keychain …")
    keyring.set_password(KEYCHAIN_SERVICE_ZOHO_CLIENT_ID, KEYCHAIN_ACCOUNT, client_id)
    print(f"  ✅ Client ID     → {KEYCHAIN_SERVICE_ZOHO_CLIENT_ID}")

    keyring.set_password(KEYCHAIN_SERVICE_ZOHO_CLIENT_SECRET, KEYCHAIN_ACCOUNT, client_secret)
    print(f"  ✅ Client Secret → {KEYCHAIN_SERVICE_ZOHO_CLIENT_SECRET}")

    keyring.set_password(KEYCHAIN_SERVICE_ZOHO_REFRESH_TOKEN, KEYCHAIN_ACCOUNT, refresh_token)
    print(f"  ✅ Refresh Token → {KEYCHAIN_SERVICE_ZOHO_REFRESH_TOKEN}")

    # Validate by refreshing
    print("\n🔄 Validating: refreshing access token …")
    resp2 = requests.post(
        ZOHO_ACCOUNTS_URL,
        params={
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
        },
        timeout=15,
    )
    data2 = resp2.json()
    if "access_token" in data2:
        print("  ✅ Token refresh works!")
    else:
        print(f"  ⚠️  Token refresh returned: {data2}")

    print("\n✅ Zoho OAuth setup complete!")


# ─── Google Ads OAuth flow ───────────────────────────────────────────────────

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GADS_REDIRECT_URI = "urn:ietf:wg:oauth:2.0:oob"
GADS_SCOPES = "https://www.googleapis.com/auth/adwords"


def setup_gads() -> None:
    """Guided Google Ads OAuth setup."""
    _ensure_keyring()
    _ensure_requests()

    print("=" * 60)
    print("  ZDROVENA – Google Ads OAuth Setup")
    print("=" * 60)
    print()

    # Step 1: Developer Token
    print("─── Step 1: Developer Token ───")
    print("  Get it from Google Ads → Tools & Settings → API Center")
    dev_token = input("  Developer Token: ").strip()
    if not dev_token:
        sys.exit("❌  Developer token is required.")
    keyring.set_password(KEYCHAIN_SERVICE_GADS_DEV_TOKEN, KEYCHAIN_ACCOUNT, dev_token)
    print("  ✅ Stored in Keychain.\n")

    # Step 2: OAuth Client Credentials
    print("─── Step 2: OAuth Client Credentials ───")
    print("  From Google Cloud Console → APIs & Services → Credentials")
    print("  (Type: Desktop application)")
    client_id = input("  Client ID: ").strip()
    client_secret = input("  Client Secret: ").strip()
    if not client_id or not client_secret:
        sys.exit("❌  Client ID and Secret are required.")

    keyring.set_password(KEYCHAIN_SERVICE_GADS_CLIENT_ID, KEYCHAIN_ACCOUNT, client_id)
    keyring.set_password(KEYCHAIN_SERVICE_GADS_CLIENT_SECRET, KEYCHAIN_ACCOUNT, client_secret)
    print("  ✅ Stored in Keychain.\n")

    # Step 3: Authorization
    print("─── Step 3: Authorization ───")
    print("  Opening browser for Google OAuth consent …\n")

    auth_params = urlencode({
        "client_id": client_id,
        "redirect_uri": GADS_REDIRECT_URI,
        "scope": GADS_SCOPES,
        "response_type": "code",
        "access_type": "offline",
        "prompt": "consent",
    })
    auth_url = f"{GOOGLE_AUTH_URL}?{auth_params}"

    print(f"  If browser doesn't open, visit this URL manually:\n  {auth_url}\n")
    webbrowser.open(auth_url)

    auth_code = input("  Paste the authorization code here: ").strip()
    if not auth_code:
        sys.exit("❌  Authorization code is required.")

    # Step 4: Exchange code for tokens
    print("\n  Exchanging authorization code for tokens …")
    resp = requests.post(
        GOOGLE_TOKEN_URL,
        data={
            "code": auth_code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": GADS_REDIRECT_URI,
            "grant_type": "authorization_code",
        },
        timeout=15,
    )

    if resp.status_code != 200:
        print(f"  ❌ Token exchange failed: {resp.status_code}")
        print(f"     {resp.text}")
        sys.exit(1)

    data = resp.json()
    refresh_token = data.get("refresh_token")
    access_token = data.get("access_token")

    if not refresh_token:
        print("  ❌ No refresh_token in response. Did you set access_type=offline?")
        print(f"     Response: {data}")
        sys.exit(1)

    keyring.set_password(KEYCHAIN_SERVICE_GADS_REFRESH_TOKEN, KEYCHAIN_ACCOUNT, refresh_token)
    print("  ✅ Refresh token stored in Keychain.")
    if access_token:
        print(f"  ℹ️  Access token (temporary): {access_token[:20]}…\n")

    # Step 5: Customer ID
    print("─── Step 4: Google Ads Customer ID ───")
    print("  This is the 10-digit number at the top of your Google Ads dashboard.")
    print("  Example: 123-456-7890 (dashes are optional)")
    cid = input("  Customer ID: ").strip()
    if cid:
        print(f"\n  ⚠️  Update zdrovena/month_closing/config.py with your Customer ID:")
        print(f'     GOOGLE_ADS_CUSTOMER_ID = "{cid.replace("-", "")}"')
        print()
        mcc = input("  Do you use a Manager (MCC) account? [y/N]: ").strip().lower()
        if mcc == "y":
            mcc_id = input("  Manager Customer ID: ").strip()
            if mcc_id:
                print(f'     GOOGLE_ADS_LOGIN_CUSTOMER_ID = "{mcc_id.replace("-", "")}"')

    print()
    print("=" * 60)
    print("  ✅  Google Ads OAuth setup complete!")
    print("=" * 60)


# ─── CLI registration ────────────────────────────────────────────────────────


def add_subparser(subparsers: Any) -> None:
    p = subparsers.add_parser(
        "setup",
        help="Konfiguracja credentiali – Keychain & OAuth",
        description=(
            "Interaktywny wizard do konfiguracji credentiali w macOS Keychain.\n\n"
            "  zdrovena setup             # pełny wizard\n"
            "  zdrovena setup --check     # sprawdź czy wszystkie sekrety istnieją\n"
            "  zdrovena setup zoho        # Zoho Mail OAuth flow\n"
            "  zdrovena setup gads        # Google Ads OAuth flow"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "target",
        nargs="?",
        choices=["zoho", "gads"],
        default=None,
        help="Konkretny flow: zoho (Zoho Mail OAuth) lub gads (Google Ads OAuth)",
    )
    p.add_argument(
        "--check",
        action="store_true",
        help="Tylko weryfikacja — sprawdź czy sekrety istnieją (bez zmian)",
    )
    p.set_defaults(func=_run)


def _run(args: argparse.Namespace) -> None:
    if args.check:
        ok = check_secrets()
        raise SystemExit(0 if ok else 1)
    elif args.target == "zoho":
        setup_zoho()
    elif args.target == "gads":
        setup_gads()
    else:
        setup_interactive()
