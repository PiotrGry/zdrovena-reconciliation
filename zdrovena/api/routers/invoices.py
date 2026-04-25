"""GET /invoices/sales   — paginated sales invoices from Fakturownia.
GET /invoices/products — product catalogue from Fakturownia.
"""

from __future__ import annotations

import os
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from zdrovena.api.auth import Principal, require_viewer_or_above
from zdrovena.api.models import InvoiceItem, ProductItem
from zdrovena.audit.api import fetch_invoices, fetch_products
from zdrovena.common.client import FakturowniaClient
from zdrovena.common.config import KEYCHAIN_SERVICE_FAKTUROWNIA
from zdrovena.common.exceptions import MissingSecretError
from zdrovena.common.secrets import get_secret

router = APIRouter(prefix="/invoices", tags=["invoices"])

_FAKTUROWNIA_DISABLED = os.environ.get("FAKTUROWNIA_DISABLED", "").lower() == "true"


def _get_fakturownia_client() -> FakturowniaClient:
    """Return an authenticated FakturowniaClient.

    Raises HTTP 503 when credentials are unavailable (e.g. dev without keychain).
    """
    if _FAKTUROWNIA_DISABLED:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Fakturownia integration is disabled (FAKTUROWNIA_DISABLED=true)",
        )
    try:
        token = get_secret(KEYCHAIN_SERVICE_FAKTUROWNIA)
    except MissingSecretError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Fakturownia credentials not configured: {exc}",
        ) from exc
    if not token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Fakturownia credentials not configured",
        )
    return FakturowniaClient(api_token=token)


@router.get(
    "/sales",
    response_model=list[InvoiceItem],
    summary="List sales invoices",
    responses={
        403: {"description": "Insufficient role"},
        503: {"description": "Fakturownia credentials not configured"},
    },
)
def list_sales_invoices(
    principal: Annotated[Principal, Depends(require_viewer_or_above)],
    year: Annotated[int, Query(ge=2020, le=2100)] = 2026,
    month: Annotated[int | None, Query(ge=1, le=12)] = None,
) -> list[InvoiceItem]:
    """Return sales invoices for the given year/month from Fakturownia API."""
    client = _get_fakturownia_client()
    invoices = fetch_invoices(client, year, month, include_proforma=False)
    return [InvoiceItem.from_fakturownia(inv) for inv in invoices]


@router.get(
    "/products",
    response_model=list[ProductItem],
    summary="List products",
    responses={
        403: {"description": "Insufficient role"},
        503: {"description": "Fakturownia credentials not configured"},
    },
)
def list_products(
    principal: Annotated[Principal, Depends(require_viewer_or_above)],
    active_only: Annotated[bool, Query(description="Return only active products")] = False,
) -> list[ProductItem]:
    """Return the product catalogue from Fakturownia API."""
    client = _get_fakturownia_client()
    products = fetch_products(client)
    if active_only:
        products = [p for p in products if not p.get("disabled")]
    return [ProductItem.from_fakturownia(p) for p in products]
