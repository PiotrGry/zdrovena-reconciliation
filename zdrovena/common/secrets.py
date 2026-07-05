"""
zdrovena.common.secrets – Unified secret resolution
=====================================================
Single entry-point for all credential lookups.

Resolution order (first non-empty value wins):
  1. Environment variable       SERVICE_NAME → SERVICE_NAME.upper()
  2. Local SOPS+age fallback    — .env.local.sops, when `sops`/`age` and a
                                   local age key are available (graceful,
                                   never raises)
  3. Azure Key Vault            — when AZURE_KEYVAULT_URL is set (Faza G)

OS keyring was dropped as a tier (it behaved inconsistently across the
multiple operating systems this project is developed from — macOS Keychain
vs. Linux Secret Service vs. Windows Credential Manager, the latter two
often unavailable on headless/sandboxed Linux boxes). The local SOPS+age
fallback replaces it: same "works without live Key Vault access" property,
but behaves identically on every OS and is git-portable (the encrypted file
can be committed and decrypted on any machine holding the age private key).

Raises MissingSecretError when required=True and no value found.
"""

from __future__ import annotations

import logging
import os
from typing import Literal, overload

from zdrovena.common.config import KEYCHAIN_ACCOUNT
from zdrovena.common.exceptions import MissingSecretError

logger = logging.getLogger("zdrovena.common.secrets")


@overload
def get_secret(service: str, required: Literal[True] = ...) -> str: ...


@overload
def get_secret(service: str, required: Literal[False]) -> str | None: ...


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
    # 1. Environment variable. Convention: uppercase and normalize hyphens
    #    to underscores so "allegro-refresh-token" -> "ALLEGRO_REFRESH_TOKEN"
    #    (env-var names cannot contain '-').
    env_key = service.upper().replace("-", "_")
    value = os.environ.get(env_key)
    if value:
        return value

    # 2. Local SOPS+age fallback (graceful — never raises; no-op unless
    #    `sops`/`age` and a local age key are configured).
    from zdrovena.common._local_secret_fallback import read_local_fallback

    value = read_local_fallback(service)
    if value:
        return value

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


def set_secret(service: str, value: str) -> bool:
    """Persist a rotated secret value.

    Resolution order (mirror of get_secret, but reversed priority — the
    most persistent store wins):

    1. Azure Key Vault (if AZURE_KEYVAULT_URL is set)
    2. Local SOPS+age fallback (best-effort; no-op unless configured)
    3. Env var override (in-process only; a warning is logged)

    Returns True if at least one persistent store accepted the write, else
    False. Never raises — the caller decides whether a failed persist is
    fatal (typically it is, for a rotated OAuth refresh token).
    """
    persisted = False

    keyvault_url = os.environ.get("AZURE_KEYVAULT_URL")
    if keyvault_url:
        from zdrovena.common._keyvault import set_keyvault_secret

        if set_keyvault_secret(keyvault_url, service, value):
            persisted = True

    from zdrovena.common._local_secret_fallback import write_local_fallback

    if write_local_fallback(service, value):
        persisted = True

    if not persisted:
        # In-process env var so this pid keeps working, but any restart is
        # doomed. Log loud so operators notice. Env var names cannot contain
        # '-' — mirror the get_secret convention of upper-casing the service
        # name and normalize any hyphens to underscores.
        os.environ[service.upper().replace("-", "_")] = value
        logger.warning(
            "Secret %s could not be persisted (no Key Vault, no local SOPS+age "
            "fallback configured). In-process env var updated — next restart "
            "WILL lose the value.",
            service,
        )

    return persisted
