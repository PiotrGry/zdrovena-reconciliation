"""zdrovena.common._keyvault — Azure Key Vault secret lookup.

Called by get_secret() when AZURE_KEYVAULT_URL is set.
Uses DefaultAzureCredential — works with:
  - Managed Identity (Container App in prod)
  - Azure CLI login (local dev: az login)
  - AZURE_CLIENT_ID / AZURE_CLIENT_SECRET env vars (service principal)

Secret name convention:
  Keychain service name  →  Key Vault secret name
  "fakturownia_api_token" →  "fakturownia-api-token"  (underscores → hyphens)

Caching:
  Secret values are cached in-memory for TTL_SECONDS (30 min) so that
  each pipeline step does not make a separate HTTP round-trip to Key Vault.
  Cache is per-process; Container App restarts clear it automatically.
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger("zdrovena.common.keyvault")

_clients: dict[str, object] = {}  # SecretClient cache per vault URL
_cache: dict[str, tuple[str, float]] = {}  # (value, expires_at) per "url:name"
TTL_SECONDS = 1800  # 30 minutes — secrets rarely change, restarts clear the cache


def _to_kv_name(service: str) -> str:
    """Convert keychain service name to Key Vault secret name."""
    return service.replace("_", "-")


def _get_client(vault_url: str) -> object:
    """Return a cached SecretClient for vault_url, creating one if needed."""
    if vault_url not in _clients:
        from azure.identity import DefaultAzureCredential
        from azure.keyvault.secrets import SecretClient

        _clients[vault_url] = SecretClient(
            vault_url=vault_url,
            credential=DefaultAzureCredential(),
        )
    return _clients[vault_url]


def get_keyvault_secret(vault_url: str, service: str) -> str | None:
    """Fetch a secret from Azure Key Vault. Returns None on any error.

    Values are cached for TTL_SECONDS to avoid a KV round-trip on every call.
    Only successful lookups are cached — failures are always retried.
    """
    try:
        from azure.keyvault.secrets import SecretClient
    except ImportError:
        logger.debug("azure-keyvault-secrets not installed — skipping Key Vault lookup")
        return None

    cache_key = f"{vault_url}:{service}"
    now = time.monotonic()
    if cache_key in _cache:
        value, expires_at = _cache[cache_key]
        if now < expires_at:
            logger.debug("Key Vault cache hit for %s", service)
            return value
        del _cache[cache_key]

    try:
        client: SecretClient = _get_client(vault_url)  # type: ignore[assignment]
        secret_name = _to_kv_name(service)
        secret = client.get_secret(secret_name)  # type: ignore[attr-defined]
        value = secret.value or None
        if value:
            _cache[cache_key] = (value, now + TTL_SECONDS)
            logger.debug("Key Vault fetched and cached %s (TTL %ds)", service, TTL_SECONDS)
        return value
    except Exception as exc:
        logger.debug("Key Vault lookup failed for %s: %s", service, exc)
        return None


def ping_keyvault(vault_url: str) -> None:
    """Verify Key Vault connectivity by listing one secret page.

    Raises an exception if the vault is unreachable or auth fails.
    Called at API startup when AZURE_KEYVAULT_URL is configured.
    """
    client: object = _get_client(vault_url)
    # list_properties_of_secrets returns a paged iterator — fetching the first
    # page is enough to verify auth + network without reading any secret values.
    next(iter(client.list_properties_of_secrets()), None)  # type: ignore[attr-defined]
