"""Fakturownia invoice preview and creation for a draft."""

from __future__ import annotations

import logging
import os
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    status,
)

from zdrovena.api import shipping_execution_composition as execution_composition
from zdrovena.api.auth import Principal, require_shipment_mgr_or_above, require_viewer_or_above
from zdrovena.api.deps import ShippingStoreDep
from zdrovena.api.models import (
    InvoiceActionResponse,
)
from zdrovena.api.routers.shipping import deps
from zdrovena.common.exceptions import MissingSecretError

logger = logging.getLogger("zdrovena.api.routers.shipping.invoices")

router = APIRouter(tags=["shipping"])


def _get_fakturownia_invoice_client() -> Any | None:
    """Build zdrovena.common.fakturownia.FakturowniaClient for invoice CRUD.

    Distinct from deps._get_fakturownia_client() which returns the audit-only
    common.client.FakturowniaClient (paginated date-range fetch only).
    """
    from zdrovena.common.config import DEFAULT_DOMAIN, KEYCHAIN_SERVICE_FAKTUROWNIA

    try:
        token = deps.get_secret(KEYCHAIN_SERVICE_FAKTUROWNIA)
    except MissingSecretError:
        return None
    from zdrovena.common.fakturownia import FakturowniaClient

    base_url = os.getenv("FAKTUROWNIA_BASE_URL", "").strip() or f"https://{DEFAULT_DOMAIN}"
    return FakturowniaClient(api_token=token, base_url=base_url)


@router.get(
    "/shipping/drafts/{draft_id}/invoice-preview",
    summary="Compute Fakturownia invoice preview for an Allegro order",
    responses={
        400: {"description": "Not an Allegro draft"},
        404: {"description": "Draft not found"},
        503: {"description": "Allegro credentials not configured"},
    },
    response_model=InvoiceActionResponse,
    response_model_exclude_unset=True,
)
def get_invoice_preview(
    draft_id: str,
    shipping_store: ShippingStoreDep,
    principal: Annotated[Principal, Depends(require_viewer_or_above)],
) -> dict[str, Any]:
    from decimal import Decimal

    draft = shipping_store.get_draft(draft_id)
    if not draft:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Draft not found")
    if draft.get("source") != "allegro":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invoice preview only for Allegro orders",
        )

    existing = draft.get("fakturownia_invoice_id")
    invoice_error = draft.get("fakturownia_invoice_error")
    if existing and not invoice_error:
        return {"status": "already_created", "fakturownia_invoice_id": existing}
    if existing and invoice_error:
        return {
            "status": "retry_ready",
            "fakturownia_invoice_id": existing,
            "error": invoice_error,
        }

    allegro_client = execution_composition.get_allegro_client()
    if allegro_client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Allegro credentials not configured",
        )

    order_id = draft.get("external_order_id") or draft.get("shopify_order_number", "")
    order = allegro_client.get_order(order_id)

    from zdrovena.common.allegro_invoice_mapper import (
        allegro_expected_payable,
        allegro_order_to_fakturownia_invoice,
    )

    payload = allegro_order_to_fakturownia_invoice(order)
    positions = payload.get("positions") or []
    settlements = payload.get("settlement_positions") or []

    positions_total = sum(Decimal(str(p.get("total_price_gross", 0))) for p in positions)
    settlement_total = sum(Decimal(str(s.get("amount", 0))) for s in settlements)
    total = positions_total + settlement_total

    buyer = order.get("buyer") or {}
    invoice_req = order.get("invoice") or {}
    addr = invoice_req.get("address") or buyer.get("address") or {}
    company = addr.get("company") or {}

    # Cross-check "Do zapłaty" (positions + kaucja) against Allegro's own
    # summary.totalToPay minus delivery (invoice has no shipping line), via the
    # shared allegro_expected_payable helper so preview and final invoice compare
    # against the identical figure. `difference` is the signed, explainable delta
    # (our total − Allegro's) so a mismatch is inspectable, not just a boolean.
    allegro_expected = allegro_expected_payable(order)
    allegro_total_to_pay: float | None = None
    matches_allegro: bool | None = None
    difference: float | None = None
    if allegro_expected is not None:
        allegro_total_to_pay = float(allegro_expected)
        delta = total - allegro_expected
        difference = float(delta)
        matches_allegro = abs(delta) <= Decimal("0.01")

    return {
        "status": "preview_ready",
        "buyer_name": payload.get(
            "buyer_name", f"{buyer.get('firstName', '')} {buyer.get('lastName', '')}".strip()
        ),
        "buyer_email": payload.get("buyer_email", buyer.get("email", "")),
        "buyer_company": company.get("name") or None,
        "buyer_nip": company.get("taxId") or None,
        "positions": [
            {
                "name": p["name"],
                "quantity": p["quantity"],
                "unit_price_gross": float(Decimal(str(p["total_price_gross"])) / p["quantity"])
                if p.get("quantity")
                else 0.0,
                "vat_rate": f"{int(p.get('tax', 0))}%",
                "line_total": float(p["total_price_gross"]),
            }
            for p in positions
        ],
        "settlement_positions": [
            {"description": s.get("description", ""), "amount": float(s.get("amount", 0) or 0)}
            for s in settlements
        ],
        "positions_total": float(positions_total),
        "settlement_total": float(settlement_total),
        "total_gross": float(total),
        "allegro_total_to_pay": allegro_total_to_pay,
        "matches_allegro": matches_allegro,
        "difference": difference,
    }


@router.post(
    "/shipping/drafts/{draft_id}/create-invoice",
    summary="Create Fakturownia invoice for an Allegro order and attach it",
    responses={
        400: {"description": "Not an Allegro draft"},
        404: {"description": "Draft not found"},
        503: {"description": "Credentials not configured"},
    },
    response_model=InvoiceActionResponse,
    response_model_exclude_unset=True,
)
def create_draft_invoice(
    draft_id: str,
    shipping_store: ShippingStoreDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
) -> dict[str, Any]:
    draft = shipping_store.get_draft(draft_id)
    if not draft:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Draft not found")
    if draft.get("source") != "allegro":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invoice creation only for Allegro orders",
        )

    existing = draft.get("fakturownia_invoice_id")
    invoice_error = draft.get("fakturownia_invoice_error")
    if existing and existing != "pending" and not invoice_error:
        return {"status": "already_created", "fakturownia_invoice_id": existing}
    if existing == "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Invoice creation already in progress — try again in a moment",
        )

    # Claim the slot optimistically so concurrent requests see "pending" and bail out.
    shipping_store.update_draft(
        draft_id,
        {"fakturownia_invoice_id": "pending", "fakturownia_invoice_error": None},
    )

    allegro_client = execution_composition.get_allegro_client()
    fakturownia_client = _get_fakturownia_invoice_client()
    if allegro_client is None:
        shipping_store.update_draft(
            draft_id,
            {
                "fakturownia_invoice_id": existing,
                "fakturownia_invoice_error": invoice_error,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Allegro credentials not configured",
        )
    if fakturownia_client is None:
        shipping_store.update_draft(
            draft_id,
            {
                "fakturownia_invoice_id": existing,
                "fakturownia_invoice_error": invoice_error,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Fakturownia credentials not configured",
        )

    order_id = draft.get("external_order_id") or draft.get("shopify_order_number", "")
    try:
        order = allegro_client.get_order(order_id)
    except Exception as exc:
        shipping_store.update_draft(
            draft_id,
            {"fakturownia_invoice_id": existing, "fakturownia_invoice_error": str(exc)},
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to fetch Allegro order: {exc}",
        ) from exc

    from zdrovena.api.routers.allegro_invoicer import create_invoice_for_order

    result = create_invoice_for_order(
        order, fakturownia_client=fakturownia_client, allegro_client=allegro_client
    )

    result_status = result.get("status")

    # "already_exists" is a success: Fakturownia already holds the invoice for
    # this order (idempotent create via oid). Persist the recovered id and
    # report "already_created" — never 502, never reset state to None (that was
    # the loop bug: clearing the slot re-armed the poller to try forever).
    if result_status == "already_exists":
        recovered_id = result.get("fakturownia_invoice_id")
        shipping_store.update_draft(
            draft_id,
            {
                "fakturownia_invoice_id": recovered_id,
                "fakturownia_invoice_number": result.get("fakturownia_invoice_number"),
                "fakturownia_invoice_error": None,
            },
        )
        return {
            "status": "already_created",
            "fakturownia_invoice_id": recovered_id,
            "fakturownia_invoice_number": result.get("fakturownia_invoice_number"),
        }

    if result_status != "created":
        # On failure, keep any invoice id Fakturownia already produced (e.g. the
        # invoice was created but the Allegro push failed) so a retry attaches to
        # the same document instead of orphaning it. Only clear the slot when we
        # truly have nothing to keep.
        recovered_id = result.get("fakturownia_invoice_id") or existing
        shipping_store.update_draft(
            draft_id,
            {
                "fakturownia_invoice_id": recovered_id,
                "fakturownia_invoice_number": result.get("fakturownia_invoice_number")
                or draft.get("fakturownia_invoice_number"),
                "fakturownia_invoice_error": result.get("error", "Invoice creation failed"),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=result.get("error", "Invoice creation failed"),
        )
    shipping_store.update_draft(
        draft_id,
        {
            "fakturownia_invoice_id": result["fakturownia_invoice_id"],
            "fakturownia_invoice_number": result.get("fakturownia_invoice_number"),
            "fakturownia_invoice_error": None,
        },
    )
    return result
