"""zdrovena.api.commands.allegro_auth_cmd — Allegro OAuth2 Device Authorization flow.

Run once to authorize the Allegro integration, or again when the refresh token
expires (every 3 months of inactivity, or after a Allegro account password change).

    zdrovena allegro-auth

Uses Device Authorization Grant (RFC 8628) — no redirect URI needed.
The user opens a URL in their browser; the CLI polls until approved, then
persists both access_token and refresh_token to Key Vault automatically.

Exit codes:
    0  — authorized successfully, tokens saved to Key Vault
    1  — authorization failed or timed out
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

logger = logging.getLogger("zdrovena.api.commands.allegro_auth")

_DEVICE_URL = "https://allegro.pl/auth/oauth/device"
_TOKEN_URL = "https://allegro.pl/auth/oauth/token"
_GRANT_DEVICE = "urn:ietf:params:oauth:grant-type:device_code"

_SANDBOX_DEVICE_URL = "https://allegro.pl.allegrosandbox.pl/auth/oauth/device"
_SANDBOX_TOKEN_URL = "https://allegro.pl.allegrosandbox.pl/auth/oauth/token"


def run(args: argparse.Namespace) -> None:
    import os

    import requests
    from requests.auth import HTTPBasicAuth

    from zdrovena.common.allegro import _SECRET_ACCESS_EXPIRY, _SECRET_ACCESS_TOKEN
    from zdrovena.common.secrets import get_secret, set_secret

    sandbox = getattr(args, "sandbox", False) or os.environ.get("ALLEGRO_ENV") == "sandbox"
    device_url = _SANDBOX_DEVICE_URL if sandbox else _DEVICE_URL
    token_url = _SANDBOX_TOKEN_URL if sandbox else _TOKEN_URL

    client_id = get_secret("allegro-client-id")
    client_secret = get_secret("allegro-client-secret")
    auth = HTTPBasicAuth(client_id, client_secret)

    env_label = "sandbox" if sandbox else "prod"
    print(f"Starting Allegro Device Authorization ({env_label})...")

    # Step 1: request device code
    try:
        r = requests.post(
            device_url,
            params={"client_id": client_id},
            auth=auth,
            timeout=15,
        )
        r.raise_for_status()
    except Exception as exc:
        logger.critical("Failed to request device code from Allegro: %s", exc)
        sys.exit(1)

    d = r.json()
    device_code = d["device_code"]
    user_code = d["user_code"]
    verification_uri = d["verification_uri"]
    interval = int(d.get("interval", 5))
    expires_in = int(d.get("expires_in", 600))

    print(f"\n{'=' * 60}")
    print("  Open this URL in your browser:")
    print(f"  {verification_uri}")
    print(f"\n  Enter this code when prompted: {user_code}")
    print(f"{'=' * 60}")
    print(f"\nWaiting for authorization (expires in {expires_in}s)...")

    # Step 2: poll until authorized or expired
    deadline = time.time() + expires_in
    while time.time() < deadline:
        time.sleep(interval)

        try:
            r = requests.post(
                token_url,
                data={"grant_type": _GRANT_DEVICE, "device_code": device_code},
                auth=auth,
                timeout=15,
            )
        except Exception as exc:
            logger.error("Network error while polling for token: %s", exc)
            continue

        if r.status_code == 200:
            t = r.json()
            access_token = t["access_token"]
            refresh_token = t["refresh_token"]
            expires_in_at = int(t.get("expires_in", 43200))
            expiry_epoch = time.time() + expires_in_at

            set_secret(_SECRET_ACCESS_TOKEN, access_token)
            set_secret(_SECRET_ACCESS_EXPIRY, str(expiry_epoch))
            set_secret("allegro-refresh-token", refresh_token)

            print("\nAuthorization successful! Tokens saved to Key Vault:")
            print(f"  allegro-access-token        (valid for {expires_in_at // 3600}h)")
            print("  allegro-refresh-token       (valid for ~3 months)")
            print("\nThe Allegro poller will auto-refresh tokens on expiry.")
            return

        error_body: dict = {}
        try:
            error_body = r.json()
        except Exception:
            pass
        error_code = error_body.get("error", "")

        if error_code == "authorization_pending":
            print(".", end="", flush=True)
        elif error_code == "slow_down":
            interval += 5
        elif error_code in ("expired_token", "access_denied"):
            print(f"\nAuthorization failed: {error_code}")
            sys.exit(1)
        else:
            print(f"\nUnexpected response {r.status_code}: {error_body}")
            sys.exit(1)

    print("\nDevice code expired before authorization was granted.")
    print("Run 'zdrovena allegro-auth' again to retry.")
    sys.exit(1)


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "allegro-auth",
        help="Authorize Allegro API access via Device Code flow (run once, or after token expiry).",
    )
    p.add_argument(
        "--sandbox",
        action="store_true",
        default=False,
        help="Use Allegro sandbox environment instead of production.",
    )
    p.set_defaults(func=run)
