"""
zdrovena.common.secrets – Unified secret resolution
=====================================================
Single entry-point for all credential lookups.

Resolution order (first non-empty value wins):
  1. Environment variable  SERVICE_NAME → SERVICE_NAME.upper()
  2. macOS Keychain (keyring)  — graceful, never raises
  3. Azure Key Vault            — when AZURE_KEYVAULT_URL is set (Faza G)

Raises MissingSecretError when required=True and no value found.
"""

from __future__ import annotations

import logging
import os

try:
    import keyring
    _KEYRING_AVAILABLE = True
except ImportError:
    _KEYRING_AVAILABLE = False

from zdrovena.common.config import KEYCHAIN_ACCOUNT
from zdrovena.common.exceptions import MissingSecretError

logger = logging.getLogger("zdrovena.common.secrets")


def get_secret(service: str, required: bool = True) -> str | None:
    """Resolve a secret by service name.

    Parameters
    ----------
    service:
        Keychain service name (e.g. ``"fakturownia_api_token"``).
        The corresponding env var is ``service.upper()``.
    required:
        If True (default) raise MissingSecretError when no value is found.
        If False return None silently.
    """
    # 1. Environment variable
    env_key = service.upper()
    value = os.environ.get(env_key)
    if value:
        return value

    # 2. macOS Keychain / system keyring (graceful — never raises)
    if _KEYRING_AVAILABLE:
        try:
            value = keyring.get_password(service, KEYCHAIN_ACCOUNT)
            if value:
                return value
        except Exception as exc:
            logger.debug("Keyring unavailable for %s: %s", service, exc)

    # 3. Azure Key Vault — activated when AZURE_KEYVAULT_URL is set
    keyvault_url = os.environ.get("AZURE_KEYVAULT_URL")
    if keyvault_url:
        from zdrovena.common._keyvault import get_keyvault_secret
        value = get_keyvault_secret(keyvault_url, service)
        if value:
            return value

    if required:
        raise MissingSecretError(service, KEYCHAIN_ACCOUNT)
    return None
