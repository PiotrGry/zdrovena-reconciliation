"""zdrovena secrets manifest — canonical list of Key Vault secret names
relevant to .env.local (the FastAPI service's local dev env file).

Single source of truth for scripts/secrets_sync.py's pull/push commands,
replacing the informal "AKV secret" table in TODOS.md. Naming matches Key
Vault's hyphenated convention (zdrovena.common._keyvault._to_kv_name
converts underscores to hyphens the same way); secrets_sync.py converts to
SCREAMING_SNAKE for .env.local's env var names.
"""

from __future__ import annotations

ENV_LOCAL_SECRETS: list[str] = [
    "allegro-client-id",
    "allegro-client-secret",
    "allegro-refresh-token",
    "shopify-webhook-secret",
    "shopify-access-token",
    "shopify-shop-domain",
    "inpost-api-token",
    "inpost-organization-id",
    "apaczka-app-id",
    "apaczka-app-secret",
    "apaczka-cod-bank-account",
    "smsapi-token",
    "notify-phone",
    "sender-name",
    "sender-street",
    "sender-building-number",
    "sender-city",
    "sender-post-code",
    "sender-phone",
    "sender-email",
    "pickup-name",
    "pickup-street",
    "pickup-building-number",
    "pickup-city",
    "pickup-post-code",
    "pickup-phone",
    "pickup-email",
]

# Secrets that must never be written back into plaintext .env.local.
#
# These rotate at runtime: Allegro hands out a new refresh token on every
# use. zdrovena.common.secrets.get_secret() checks environment variables
# FIRST, so a copy sitting in .env.local (which docker-compose loads via
# env_file) would permanently shadow the SOPS+age tier that persists the
# rotation — the process would keep reading a token that Allegro has
# already invalidated. Their only home is .env.local.sops, written by
# set_secret(). See docs/devops/sops-age.md §3.
SOPS_ONLY_SECRETS: frozenset[str] = frozenset(
    {
        "allegro-refresh-token",
        "allegro-access-token",
        "allegro-access-token-expiry",
    }
)
