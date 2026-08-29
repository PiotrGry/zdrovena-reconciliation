"""Shared composition and environment gating for the shipping routers.

Kept in one place so every shipping router shares one way of reaching
secrets and provider clients, instead of each growing its own (#313).
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import (
    HTTPException,
    status,
)

from zdrovena.common.appenv import is_production_env
from zdrovena.common.exceptions import MissingSecretError
from zdrovena.common.secrets import get_secret

logger = logging.getLogger("zdrovena.api.routers.shipping.deps")


def _is_production_env() -> bool:
    """True when the canonical ``APP_ENV`` signals a production deploy.

    Delegates to :func:`zdrovena.common.appenv.is_production_env` so the whole
    application resolves "is this production?" from one canonical place (R4-B).
    """
    return is_production_env()


def _test_support_enabled() -> bool:
    return os.getenv("PROVIDER_MODE", "").strip().lower() == "fake" and not _is_production_env()


def _require_test_support() -> None:
    if not _test_support_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


def _is_e2e_record(record: dict[str, Any]) -> bool:
    record_id = str(record.get("id") or "")
    order_number = str(
        record.get("shopify_order_number")
        or record.get("order_number")
        or (record.get("payload") or {}).get("order_number")
        or ""
    )
    return record_id.startswith("e2e-") or order_number.startswith("990")


def _get_fakturownia_client() -> Any | None:
    """Build a FakturowniaClient from Key Vault secrets. Returns None if missing."""
    from zdrovena.common.config import KEYCHAIN_SERVICE_FAKTUROWNIA

    try:
        token = get_secret(KEYCHAIN_SERVICE_FAKTUROWNIA)
    except MissingSecretError:
        return None
    from zdrovena.common.client import FakturowniaClient

    return FakturowniaClient(api_token=token)


def _allowed_shopify_domains() -> frozenset[str] | None:
    """Whitelisted shop domains from SHOPIFY_ALLOWED_DOMAINS (comma-separated).

    Returns None when unset — dev mode, all domains accepted (with a warning).
    """
    raw = os.getenv("SHOPIFY_ALLOWED_DOMAINS", "").strip()
    if not raw:
        return None
    return frozenset(d.strip().lower() for d in raw.split(",") if d.strip())
