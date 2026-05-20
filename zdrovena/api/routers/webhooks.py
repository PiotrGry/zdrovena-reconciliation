"""zdrovena.api.routers.webhooks — Shopify webhooks + shipping drafts + label endpoints.

POST /webhooks/shopify/order-created      — Shopify order webhook (HMAC-validated)
GET  /shipping/drafts                     — list shipping drafts from Table Storage
GET  /shipping/drafts/{id}/label          — stream label PDF from courier
POST /shipping/drafts/{id}/execute        — (re)create courier shipment for a draft
POST /shipping/drafts/{id}/pickup         — order InPost kurier pickup
PATCH /shipping/drafts/{id}               — update packages_count
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from zdrovena.api.auth import Principal, require_shipment_mgr_or_above, require_viewer_or_above
from zdrovena.api.deps import ShippingStoreDep, StorageDep
from zdrovena.common.secrets import get_secret
from zdrovena.common.shipping_store import ShippingStore

logger = logging.getLogger("zdrovena.api.routers.webhooks")

router = APIRouter(tags=["shipping"])


# ── HMAC helpers ──────────────────────────────────────────────────────────────


def _verify_shopify_hmac(raw_body: bytes, signature_header: str, secret: str) -> bool:
    computed = base64.b64encode(
        hmac.new(secret.encode(), raw_body, hashlib.sha256).digest()
    ).decode()
    return hmac.compare_digest(computed, signature_header)


def _get_webhook_secret() -> str | None:
    return get_secret("shopify_webhook_secret", required=False)


# ── Sender address ────────────────────────────────────────────────────────────


def _get_sender() -> dict[str, str]:
    return {
        "name": get_secret("sender_name", required=False) or "",
        "firstname": "",
        "lastname": get_secret("sender_name", required=False) or "",
        "street": get_secret("sender_street", required=False) or "",
        "building_number": "1",
        "city": get_secret("sender_city", required=False) or "",
        "post_code": get_secret("sender_post_code", required=False) or "",
        "phone": get_secret("sender_phone", required=False) or "",
        "email": get_secret("sender_email", required=False) or "",
    }


# ── Routing: decide courier from shipping_lines title ─────────────────────────


def _pick_courier(order: dict[str, Any]) -> str:
    lines = order.get("shipping_lines") or []
    title = (lines[0].get("title", "") if lines else "").lower()
    if "kurier" in title or "paczkomat" in title:
        return "inpost"
    return "apaczka"


def _pick_inpost_service(title: str) -> str:
    return "paczkomat" if "paczkomat" in title.lower() else "kurier"


# ── Courier execution helpers ─────────────────────────────────────────────────


def _run_inpost(draft: dict[str, Any], sender: dict[str, str]) -> dict[str, Any]:
    """Create or recreate InPost shipment from stored draft fields. Returns patch dict."""
    from zdrovena.common.inpost import InPostClient

    token = get_secret("inpost_api_token")
    org_id = get_secret("inpost_organization_id")
    client = InPostClient(token, org_id)

    receiver = draft.get("receiver") or {}
    first_name = receiver.get("first_name", "")
    last_name = receiver.get("last_name", "")
    email = receiver.get("email", "")
    phone = receiver.get("phone", "")
    reference = str(draft.get("shopify_order_number", ""))
    inpost_service = "paczkomat" if draft.get("service") == "inpost_locker_standard" else "kurier"

    if inpost_service == "paczkomat":
        result = client.create_paczkomat_shipment(
            receiver_first_name=first_name,
            receiver_last_name=last_name,
            receiver_email=email,
            receiver_phone=phone,
            target_point=receiver.get("locker_id", ""),
            reference=reference,
        )
    else:
        addr = draft.get("shipping_address") or {}
        result = client.create_kurier_shipment(
            receiver_first_name=first_name,
            receiver_last_name=last_name,
            receiver_email=email,
            receiver_phone=phone,
            receiver_street=addr.get("street", ""),
            receiver_building_number="1",
            receiver_city=addr.get("city", ""),
            receiver_post_code=addr.get("post_code", ""),
            sender=sender,
            reference=reference,
        )
        try:
            client.create_dispatch_order(str(result["id"]), sender)
        except Exception as exc:
            logger.warning("InPost dispatch order failed for %s: %s", reference, exc)

    return {
        "courier_draft_id": str(result.get("id", "")),
        "tracking_number": result.get("tracking_number"),
        "status": "created",
        "error": None,
    }


def _run_apaczka(draft: dict[str, Any], sender: dict[str, str], storage: Any) -> dict[str, Any]:
    """Create or recreate Apaczka shipment from stored draft fields. Returns patch dict."""
    from zdrovena.common.apaczka import ApaczkaClient

    app_id = get_secret("apaczka_app_id")
    app_secret = get_secret("apaczka_app_secret")
    service_id = get_secret("apaczka_service_id")
    client = ApaczkaClient(app_id, app_secret, service_id, storage)

    receiver = draft.get("receiver") or {}
    addr = draft.get("shipping_address") or {}
    customer_name = f"{receiver.get('first_name', '')} {receiver.get('last_name', '')}".strip()
    result = client.create_shipment(
        receiver_name=customer_name,
        receiver_firstname=receiver.get("first_name", ""),
        receiver_lastname=receiver.get("last_name", ""),
        receiver_email=receiver.get("email", ""),
        receiver_phone=receiver.get("phone", ""),
        receiver_address=addr.get("street", ""),
        receiver_city=addr.get("city", ""),
        receiver_zip=addr.get("post_code", ""),
        sender=sender,
        reference=str(draft.get("shopify_order_number", "")),
    )
    return {
        "courier_draft_id": str(result.get("id", "")),
        "tracking_number": result.get("waybill_number"),
        "status": "created",
        "error": None,
    }


# ── Background task: create draft on Shopify webhook ─────────────────────────


def _create_draft(order: dict[str, Any], shipping_store: ShippingStore, storage: Any) -> None:
    order_id = str(order.get("id", ""))
    order_number = order.get("order_number") or order.get("name", "")
    shipping_lines = order.get("shipping_lines") or []
    title = shipping_lines[0].get("title", "") if shipping_lines else ""

    shipping_addr = order.get("shipping_address") or {}
    customer = order.get("customer") or {}
    first_name = shipping_addr.get("first_name") or customer.get("first_name", "")
    last_name = shipping_addr.get("last_name") or customer.get("last_name", "")
    customer_name = f"{first_name} {last_name}".strip()
    email = order.get("email") or customer.get("email", "")
    phone = shipping_addr.get("phone") or order.get("phone") or customer.get("phone", "")

    courier = _pick_courier(order)
    inpost_service = _pick_inpost_service(title) if courier == "inpost" else None

    record: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": "shopify",
        "shopify_order_id": order_id,
        "shopify_order_number": str(order_number),
        "customer_name": customer_name,
        "courier": courier,
        "service": None,
        "tracking_number": None,
        "courier_draft_id": None,
        "status": "error",
        "packages_count": 1,
        "pickup_ordered": False,
        "receiver": {
            "first_name": first_name,
            "last_name": last_name,
            "email": email,
            "phone": phone,
            "locker_id": "",
        },
        "shipping_address": {
            "street": f"{shipping_addr.get('address1', '')} {shipping_addr.get('address2', '')}".strip(),
            "city": shipping_addr.get("city", ""),
            "post_code": shipping_addr.get("zip", ""),
        },
        "parcel": {"template": "small", "weight_kg": None},
        "error": None,
    }

    # Set service before API calls so it's always persisted even on credential error
    if courier == "inpost":
        record["service"] = (
            "inpost_locker_standard" if inpost_service == "paczkomat" else "inpost_courier_standard"
        )
    else:
        record["service"] = "apaczka"

    try:
        sender = _get_sender()

        if courier == "inpost":
            from zdrovena.common.inpost import InPostClient

            token = get_secret("inpost_api_token")
            org_id = get_secret("inpost_organization_id")
            client = InPostClient(token, org_id)

            if inpost_service == "paczkomat":
                note_attrs = {a["name"]: a["value"] for a in (order.get("note_attributes") or [])}
                locker_id = (
                    note_attrs.get("inpost_locker_id")
                    or note_attrs.get("paczkomat_id")
                    or note_attrs.get("locker_id")
                    or shipping_addr.get("address2", "")
                )
                record["receiver"]["locker_id"] = locker_id
                result = client.create_paczkomat_shipment(
                    receiver_first_name=first_name,
                    receiver_last_name=last_name,
                    receiver_email=email,
                    receiver_phone=phone,
                    target_point=locker_id,
                    reference=str(order_number),
                )
            else:
                street = shipping_addr.get("address1", "")
                building = shipping_addr.get("address2", "")
                result = client.create_kurier_shipment(
                    receiver_first_name=first_name,
                    receiver_last_name=last_name,
                    receiver_email=email,
                    receiver_phone=phone,
                    receiver_street=street,
                    receiver_building_number=building or "1",
                    receiver_city=shipping_addr.get("city", ""),
                    receiver_post_code=shipping_addr.get("zip", ""),
                    sender=sender,
                    reference=str(order_number),
                )
                try:
                    client.create_dispatch_order(str(result["id"]), sender)
                    record["pickup_ordered"] = True
                except Exception as exc:
                    logger.warning("InPost dispatch order failed for %s: %s", order_number, exc)

            record["courier_draft_id"] = str(result.get("id", ""))
            record["tracking_number"] = result.get("tracking_number")
            record["status"] = "created"

        else:  # apaczka
            from zdrovena.common.apaczka import ApaczkaClient

            app_id = get_secret("apaczka_app_id")
            app_secret = get_secret("apaczka_app_secret")
            service_id = get_secret("apaczka_service_id")
            client_a = ApaczkaClient(app_id, app_secret, service_id, storage)

            street = (
                f"{shipping_addr.get('address1', '')} {shipping_addr.get('address2', '')}".strip()
            )
            result = client_a.create_shipment(
                receiver_name=customer_name,
                receiver_firstname=first_name,
                receiver_lastname=last_name,
                receiver_email=email,
                receiver_phone=phone,
                receiver_address=street,
                receiver_city=shipping_addr.get("city", ""),
                receiver_zip=shipping_addr.get("zip", ""),
                sender=sender,
                reference=str(order_number),
            )
            record["courier_draft_id"] = str(result.get("id", ""))
            record["tracking_number"] = result.get("waybill_number")
            record["status"] = "created"
            record["parcel"]["weight_kg"] = 1.0

    except Exception as exc:
        logger.error(
            "Shipping draft creation failed for order %s (%s): %s", order_number, courier, exc
        )
        record["error"] = str(exc)

    shipping_store.upsert_draft(record)


# ── Webhook endpoint ──────────────────────────────────────────────────────────


@router.post(
    "/webhooks/shopify/order-created",
    status_code=status.HTTP_200_OK,
    summary="Shopify order webhook — creates shipping draft",
    include_in_schema=False,
)
async def shopify_order_created(
    request: Request,
    background_tasks: BackgroundTasks,
    shipping_store: ShippingStoreDep,
    storage: StorageDep,
) -> dict[str, str]:
    raw_body = await request.body()

    sig_header = request.headers.get("X-Shopify-Hmac-Sha256", "")
    webhook_secret = _get_webhook_secret()

    if webhook_secret:
        if not sig_header:
            logger.warning("Shopify webhook received without HMAC header — rejected")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing signature"
            )
        if not _verify_shopify_hmac(raw_body, sig_header, webhook_secret):
            logger.warning("Shopify webhook HMAC mismatch — rejected")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature"
            )
    else:
        logger.warning("shopify-webhook-secret not configured — skipping HMAC validation")

    try:
        order = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON") from exc

    if not order.get("shipping_lines"):
        logger.warning("Order %s has no shipping_lines — skipping draft", order.get("id"))
        return {"status": "skipped"}

    background_tasks.add_task(_create_draft, order, shipping_store, storage)
    logger.info("Queued shipping draft for order %s", order.get("order_number") or order.get("id"))
    return {"status": "accepted"}


# ── Drafts list ───────────────────────────────────────────────────────────────


@router.get(
    "/shipping/drafts",
    summary="List shipping drafts",
    responses={403: {"description": "Insufficient role"}},
)
def list_drafts(
    shipping_store: ShippingStoreDep,
    principal: Annotated[Principal, Depends(require_viewer_or_above)],
) -> dict[str, Any]:
    drafts = shipping_store.list_drafts()
    return {"drafts": drafts}


# ── Execute draft ─────────────────────────────────────────────────────────────


@router.post(
    "/shipping/drafts/{draft_id}/execute",
    summary="(Re)create courier shipment for a draft",
    responses={
        403: {"description": "Insufficient role"},
        404: {"description": "Draft not found"},
        409: {"description": "Draft already executed"},
    },
)
def execute_draft(
    draft_id: str,
    shipping_store: ShippingStoreDep,
    storage: StorageDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
) -> dict[str, Any]:
    draft = shipping_store.get_draft(draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")
    if draft.get("status") == "created":
        raise HTTPException(status_code=409, detail="Draft already executed")

    try:
        sender = _get_sender()
        courier = draft.get("courier", "apaczka")
        if courier == "inpost":
            patch = _run_inpost(draft, sender)
        else:
            patch = _run_apaczka(draft, sender, storage)
    except Exception as exc:
        logger.error("execute_draft failed for %s: %s", draft_id, exc)
        shipping_store.update_draft(draft_id, {"status": "error", "error": str(exc)})
        raise HTTPException(status_code=502, detail=f"Courier API error: {exc}") from exc

    shipping_store.update_draft(draft_id, patch)
    updated = shipping_store.get_draft(draft_id)
    return updated or patch


# ── Pickup (InPost kurier only) ───────────────────────────────────────────────


@router.post(
    "/shipping/drafts/{draft_id}/pickup",
    summary="Order InPost kurier pickup for an executed draft",
    responses={
        403: {"description": "Insufficient role"},
        404: {"description": "Draft not found"},
        409: {"description": "Pickup already ordered or draft not ready"},
        400: {"description": "Courier does not support pickup (not InPost kurier)"},
    },
)
def order_pickup(
    draft_id: str,
    shipping_store: ShippingStoreDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
) -> dict[str, Any]:
    draft = shipping_store.get_draft(draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")
    if draft.get("courier") != "inpost" or draft.get("service") != "inpost_courier_standard":
        raise HTTPException(status_code=400, detail="Pickup only available for InPost kurier")
    if draft.get("status") != "created":
        raise HTTPException(status_code=409, detail="Draft must be in 'created' state")
    if draft.get("pickup_ordered"):
        raise HTTPException(status_code=409, detail="Pickup already ordered")

    courier_draft_id = draft.get("courier_draft_id")
    if not courier_draft_id:
        raise HTTPException(status_code=409, detail="No courier draft ID — execute first")

    try:
        from zdrovena.common.inpost import InPostClient

        token = get_secret("inpost_api_token")
        org_id = get_secret("inpost_organization_id")
        client = InPostClient(token, org_id)
        sender = _get_sender()
        client.create_dispatch_order(courier_draft_id, sender)
    except Exception as exc:
        logger.error("order_pickup failed for draft %s: %s", draft_id, exc)
        raise HTTPException(status_code=502, detail=f"InPost dispatch error: {exc}") from exc

    shipping_store.update_draft(draft_id, {"pickup_ordered": True})
    return {"status": "pickup_ordered", "draft_id": draft_id}


# ── Update packages_count ─────────────────────────────────────────────────────


@router.patch(
    "/shipping/drafts/{draft_id}",
    summary="Update draft metadata (packages_count)",
    responses={
        403: {"description": "Insufficient role"},
        404: {"description": "Draft not found"},
    },
)
def update_draft(
    draft_id: str,
    shipping_store: ShippingStoreDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
    packages_count: int = Body(..., ge=1, le=99, embed=True),
) -> dict[str, Any]:
    draft = shipping_store.get_draft(draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")
    shipping_store.update_draft(draft_id, {"packages_count": packages_count})
    return {"draft_id": draft_id, "packages_count": packages_count}


# ── Label streaming ───────────────────────────────────────────────────────────


@router.get(
    "/shipping/drafts/{draft_id}/label",
    summary="Stream shipping label PDF",
    responses={403: {"description": "Insufficient role"}, 404: {"description": "Draft not found"}},
)
def get_label(
    draft_id: str,
    shipping_store: ShippingStoreDep,
    storage: StorageDep,
    principal: Annotated[Principal, Depends(require_viewer_or_above)],
    courier: str = Query(..., description="inpost or apaczka"),
) -> StreamingResponse:
    draft = shipping_store.get_draft(draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")

    courier_draft_id = draft.get("courier_draft_id")
    if not courier_draft_id:
        raise HTTPException(status_code=404, detail="No courier draft ID — draft may have failed")

    try:
        if courier == "inpost":
            from zdrovena.common.inpost import InPostClient

            token = get_secret("inpost_api_token")
            org_id = get_secret("inpost_organization_id")
            pdf_bytes = InPostClient(token, org_id).get_label(courier_draft_id)
        elif courier == "apaczka":
            from zdrovena.common.apaczka import ApaczkaClient

            app_id = get_secret("apaczka_app_id")
            app_secret = get_secret("apaczka_app_secret")
            service_id = get_secret("apaczka_service_id")
            pdf_bytes = ApaczkaClient(app_id, app_secret, service_id, storage).get_label(
                courier_draft_id
            )
        else:
            raise HTTPException(status_code=400, detail=f"Unknown courier: {courier}")
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Label fetch failed for draft %s: %s", draft_id, exc)
        raise HTTPException(status_code=502, detail=f"Courier API error: {exc}") from exc

    order_num = draft.get("shopify_order_number", draft_id).lstrip("#")
    filename = f"label_{courier}_{order_num}.pdf"
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )
