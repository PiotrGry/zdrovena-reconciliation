"""Turning a draft into a courier shipment, confirming it, marking it fulfilled."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Body,
    Depends,
    HTTPException,
    Query,
)
from fastapi.responses import JSONResponse

from zdrovena.api import shipping_execution_composition as execution_composition
from zdrovena.api.auth import Principal, require_shipment_mgr_or_above
from zdrovena.api.deps import ShippingStoreDep, StorageDep
from zdrovena.api.models import (
    MarkFulfilledResponse,
    ShipmentActionResponse,
)
from zdrovena.api.routers.shipping import deps
from zdrovena.common.shipping_exceptions import (
    AllegroAuthError,
    AllegroBusinessError,
    ApaczkaBusinessError,
    CourierTransientError,
)
from zdrovena.shipping.application.execution import workflow as execution_workflow

logger = logging.getLogger("zdrovena.api.routers.shipping.execution")

router = APIRouter(tags=["shipping"])


_SHOPIFY_COURIER_COMPANY: dict[str, str] = {
    "inpost": "InPost",
    "apaczka": "Apaczka",
    "allegro_delivery": "Allegro Delivery",
    "allegro": "Allegro Delivery",
}


_SHOPIFY_COURIER_TRACKING_URL: dict[str, str] = {
    "inpost": "https://inpost.pl/sledzenie-przesylek?number={number}",
}


def _sync_shopify_fulfillment(
    order_id: str,
    tracking_number: str | None,
    courier: str | None,
) -> dict[str, Any]:
    """Create a Shopify fulfillment for a completed order via the FulfillmentOrder API.

    Non-blocking: caller decides whether to surface failures as warnings or errors.
    Returns a result dict with "created", "skipped", or "error" key.
    """
    import requests

    shopify_token = deps.get_secret("shopify_admin_token", required=False)
    if not shopify_token:
        return {"skipped": "shopify_not_configured"}

    allowed_domains = deps._allowed_shopify_domains()
    if not allowed_domains:
        return {"skipped": "no_shopify_domain"}

    shop_domain = next(iter(allowed_domains))
    headers = {
        "X-Shopify-Access-Token": shopify_token,
        "Content-Type": "application/json",
    }
    base = f"https://{shop_domain}/admin/api/2024-01"

    # Step 1: find open fulfillment orders (the modern Shopify fulfillment model)
    fo_resp = requests.get(
        f"{base}/orders/{order_id}/fulfillment_orders.json",
        headers=headers,
        timeout=15,
    )
    fo_resp.raise_for_status()
    open_fo_ids = [
        fo["id"]
        for fo in fo_resp.json().get("fulfillment_orders", [])
        if fo.get("status") == "open"
    ]
    if not open_fo_ids:
        return {"skipped": "no_open_fulfillment_orders"}

    # Step 2: create fulfillment with tracking info
    courier_key = (courier or "").lower()
    tracking_company = _SHOPIFY_COURIER_COMPANY.get(courier_key, courier or "")
    tracking_url_tpl = _SHOPIFY_COURIER_TRACKING_URL.get(courier_key)
    tracking_url = (
        tracking_url_tpl.format(number=tracking_number)
        if tracking_url_tpl and tracking_number
        else None
    )

    payload: dict[str, Any] = {
        "fulfillment": {
            "line_items_by_fulfillment_order": [
                {"fulfillment_order_id": fo_id} for fo_id in open_fo_ids
            ],
            "notify_customer": True,
        }
    }
    if tracking_number:
        tracking_info: dict[str, Any] = {"number": tracking_number, "company": tracking_company}
        if tracking_url:
            tracking_info["url"] = tracking_url
        payload["fulfillment"]["tracking_info"] = tracking_info

    f_resp = requests.post(f"{base}/fulfillments.json", headers=headers, json=payload, timeout=15)
    f_resp.raise_for_status()
    fulfillment = f_resp.json().get("fulfillment", {})
    return {
        "created": True,
        "shopify_fulfillment_id": str(fulfillment.get("id", "")),
        "tracking_number": tracking_number,
    }


@router.get(
    "/shipping/drafts/{draft_id}/execute/preview",
    summary="Show exactly what would be sent to the courier, without sending it",
    responses={
        403: {"description": "Insufficient role"},
        404: {"description": "Draft not found"},
    },
    response_model=ShipmentActionResponse,
    response_model_exclude_unset=True,
)
def preview_execute_draft(
    draft_id: str,
    shipping_store: ShippingStoreDep,
    storage: StorageDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
    pickup_date: str | None = Query(None),
    pickup_from: str | None = Query(None),
    pickup_to: str | None = Query(None),
) -> dict[str, Any]:
    draft = shipping_store.get_draft(draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")
    try:
        return execution_composition.execution_preview(
            draft,
            storage=storage,
            pickup_date=pickup_date,
            pickup_from=pickup_from,
            pickup_to=pickup_to,
        )
    except ApaczkaBusinessError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post(
    "/shipping/drafts/{draft_id}/execute",
    summary="(Re)create courier shipment for a draft",
    responses={
        403: {"description": "Insufficient role"},
        404: {"description": "Draft not found"},
        409: {"description": "Draft already executed"},
    },
    response_model=ShipmentActionResponse,
    response_model_exclude_unset=True,
)
def execute_draft(
    draft_id: str,
    shipping_store: ShippingStoreDep,
    storage: StorageDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
    pickup_date: str | None = Body(None),
    pickup_from: str | None = Body(None),
    pickup_to: str | None = Body(None),
    preview_fingerprint: str | None = Body(None),
) -> dict[str, Any]:
    try:
        return execution_composition.execute_shipping_draft(
            draft_id,
            shipping_store,
            storage,
            pickup_window=execution_workflow.PickupWindow(
                date=pickup_date,
                from_time=pickup_from,
                to_time=pickup_to,
            ),
            preview_fingerprint=preview_fingerprint,
        )
    except execution_composition.EXECUTION_APPLICATION_HTTP_ERRORS as exc:
        execution_composition.raise_execution_http_exception(exc)
    except ApaczkaBusinessError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post(
    "/shipping/drafts/{draft_id}/confirm",
    summary="Poll Allegro create-command and finalise a pending_confirmation draft",
    responses={
        403: {"description": "Insufficient role"},
        404: {"description": "Draft not found"},
        409: {"description": "Draft not in pending_confirmation state"},
        202: {"description": "Still pending"},
        502: {"description": "Allegro API error"},
    },
    response_model=ShipmentActionResponse,
    response_model_exclude_unset=True,
)
def confirm_pending_command(
    draft_id: str,
    shipping_store: ShippingStoreDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
) -> Any:
    """Poll an outstanding Allegro create-command and finalise the draft.

    Ship-with-Allegro create-commands are asynchronous. ``execute_draft`` returns
    ``pending_confirmation`` when the command is still IN_PROGRESS after the
    short in-request polling window. This endpoint is the durable follow-up:
    call it (via UI action or a cron/worker) to check the command status and
    either promote the draft to ``created`` (SUCCESS) or ``error`` (ERROR).

    Idempotent: safe to call multiple times. Returns the current draft.
    """
    try:
        result = execution_composition.confirm_shipping_draft(draft_id, shipping_store)
    except execution_composition.ConfirmationError as exc:
        execution_composition.raise_confirmation_http_exception(exc)
    if result.status_code == 202:
        return JSONResponse(status_code=202, content=result.payload)
    return result.payload


@router.post(
    "/shipping/drafts/{draft_id}/mark-fulfilled",
    summary="Manually mark the draft as fulfilled (operator action)",
    responses={
        403: {"description": "Insufficient role"},
        404: {"description": "Draft not found"},
        409: {"description": "Allegro draft has no external Allegro order id"},
        502: {"description": "Allegro API error (only for Allegro drafts)"},
    },
    response_model=MarkFulfilledResponse,
    response_model_exclude_unset=True,
)
def mark_fulfilled(
    draft_id: str,
    shipping_store: ShippingStoreDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
) -> dict[str, Any]:
    """Idempotent operator action to mark the draft as fulfilled.

    A draft only represents "we intend to ship", not "we shipped". The operator
    confirms via the UI once the parcel actually leaves — this endpoint sets the
    local ``fulfillment_status="fulfilled"`` flag (with ``fulfilled_at`` /
    ``fulfilled_by``) for every draft, regardless of source.

    For Allegro drafts we additionally invoke
    ``AllegroClient.mark_order_processed(external_order_id, status="SENT")`` to
    move the order to ``SENT`` on Allegro's side (the parcel has left), and mirror
    the timestamps into the legacy ``allegro_fulfillment_status`` /
    ``allegro_marked_processed_*`` fields.

    Re-running this endpoint is safe: if the draft is already fulfilled we
    return 200 without hitting Allegro again.
    """
    draft = shipping_store.get_draft(draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")

    # R5-A: a cancelled or errored draft was never successfully shipped, so it
    # must not be marked fulfilled (that would push a bogus SENT to Allegro).
    # Re-running on an already-fulfilled draft stays idempotent (handled below).
    if (
        draft.get("status") in ("cancelled", "error")
        and draft.get("fulfillment_status") != "fulfilled"
    ):
        raise HTTPException(
            status_code=409,
            detail="Nie można oznaczyć jako zrealizowane: przesyłka jest anulowana lub w błędzie.",
        )

    is_allegro = draft.get("source") == "allegro"
    external_order_id = (
        draft.get("external_order_id") or draft.get("allegro_order_id") if is_allegro else None
    )
    if is_allegro and not external_order_id:
        raise HTTPException(status_code=409, detail="Allegro draft has no external order id")

    # Idempotency - a second click is a no-op that reports the existing state.
    if draft.get("fulfillment_status") == "fulfilled":
        return {
            "status": "already_fulfilled",
            "draft_id": draft_id,
            "source": draft.get("source"),
            "external_order_id": external_order_id,
            "fulfilled_at": draft.get("fulfilled_at"),
            "fulfilled_by": draft.get("fulfilled_by"),
            "allegro_side_effect": False,
            "shopify_side_effect": None,
        }

    allegro_side_effect = False
    if is_allegro and not execution_composition.MOCK_COURIER:
        client = execution_composition.get_allegro_client()
        if client is None:
            raise HTTPException(status_code=502, detail="Allegro credentials missing")
        try:
            client.mark_order_processed(str(external_order_id), status="SENT")
            allegro_side_effect = True
        except (AllegroBusinessError, AllegroAuthError, CourierTransientError) as exc:
            logger.exception("Allegro mark_order_processed failed for draft %s", draft_id)
            raise HTTPException(status_code=502, detail=f"Allegro API error: {exc}") from exc
    elif is_allegro and execution_composition.MOCK_COURIER:
        # In mock mode we still record that the Allegro side-effect "happened".
        allegro_side_effect = True

    marked_at = datetime.now(timezone.utc).isoformat()
    marked_by = principal.email or principal.sub

    patch: dict[str, Any] = {
        "fulfillment_status": "fulfilled",
        "fulfilled_at": marked_at,
        "fulfilled_by": marked_by,
    }
    if is_allegro:
        # Keep the Allegro-specific mirror fields for backwards compatibility
        # with any UI/report that already reads them.
        patch["allegro_fulfillment_status"] = "SENT"
        patch["allegro_marked_processed_at"] = marked_at
        patch["allegro_marked_processed_by"] = marked_by

    shipping_store.update_draft(draft_id, patch)

    shopify_side_effect: dict[str, Any] | None = None
    is_shopify = draft.get("source") == "shopify"
    if is_shopify:
        shopify_order_id = str(
            draft.get("external_order_id") or draft.get("shopify_order_id") or ""
        )
        if shopify_order_id:
            try:
                shopify_side_effect = _sync_shopify_fulfillment(
                    order_id=shopify_order_id,
                    tracking_number=draft.get("tracking_number"),
                    courier=draft.get("courier"),
                )
            except Exception as exc:
                logger.exception("Shopify fulfillment sync failed for draft %s", draft_id)
                shopify_side_effect = {"error": str(exc)}

    return {
        "status": "marked_fulfilled",
        "draft_id": draft_id,
        "source": draft.get("source"),
        "external_order_id": external_order_id,
        "fulfilled_at": marked_at,
        "fulfilled_by": marked_by,
        "allegro_side_effect": allegro_side_effect,
        "shopify_side_effect": shopify_side_effect,
    }
