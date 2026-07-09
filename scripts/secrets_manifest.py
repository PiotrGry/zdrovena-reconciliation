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
    "smsapi-token",
    "notify-phone",
    "sender-name",
    "sender-street",
    "sender-building-number",
    "sender-city",
    "sender-post-code",
    "sender-phone",
    "sender-email",
]
