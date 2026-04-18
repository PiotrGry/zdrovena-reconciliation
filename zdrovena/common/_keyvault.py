"""zdrovena.common._keyvault — Azure Key Vault secret lookup.

Called by get_secret() when AZURE_KEYVAULT_URL is set.
Uses DefaultAzureCredential — works with:
  - Managed Identity (Container App in prod)
  - Azure CLI login (local dev: az login)
  - AZURE_CLIENT_ID / AZURE_CLIENT_SECRET env vars (service principal)

Secret name convention:
  Keychain service name  →  Key Vault secret name
  "fakturownia_api_token" →  "fakturownia-api-token"  (underscores → hyphens)
"""

from __future__ import annotations

import logging

logger = logging.getLogger("zdrovena.common.keyvault")

_clients: dict[str, object] = {}  # cache per vault URL


def _to_kv_name(service: str) -> str:
    """Convert keychain service name to Key Vault secret name."""
    return service.replace("_", "-")


def get_keyvault_secret(vault_url: str, service: str) -> str | None:
    """Fetch a secret from Azure Key Vault. Returns None on any error."""
    try:
        from azure.identity import DefaultAzureCredential
        from azure.keyvault.secrets import SecretClient
    except ImportError:
        logger.debug("azure-keyvault-secrets not installed — skipping Key Vault lookup")
        return None

    try:
        if vault_url not in _clients:
            _clients[vault_url] = SecretClient(
                vault_url=vault_url,
                credential=DefaultAzureCredential(),
            )
        client: SecretClient = _clients[vault_url]  # type: ignore[assignment]
        secret_name = _to_kv_name(service)
        secret = client.get_secret(secret_name)
        return secret.value or None
    except Exception as exc:
        logger.debug("Key Vault lookup failed for %s: %s", service, exc)
        return None
