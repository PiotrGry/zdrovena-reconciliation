"""zdrovena.api.routers.webhooks — Shopify webhooks + shipping drafts + label endpoints.

POST /webhooks/shopify/order-created  — Shopify order webhook (HMAC-validated)
GET  /shipping/drafts                 — list shipping drafts from blob
GET  /shipping/drafts/{id}/label      — stream label PDF from courier
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

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from zdrovena.api.auth import Principal, require_viewer_or_above
from zdrovena.api.deps import StorageDep
from zdrovena.common.secrets import get_secret

logger = logging.getLogger("zdrovena.api.routers.webhooks")

router = APIRouter(tags=["shipping"])

_DRAFTS_KEY = "shipping/drafts.jsonl"


# ── HMAC helpers ──────────────────────────────────────────────────────────────


def _verify_shopify_hmac(raw_body: bytes, signature_header: str, secret: str) -> bool:
    computed = base64.b64encode(
        hmac.new(secret.encode(), raw_body, hashlib.sha256).digest()
    ).decode()
    return hmac.compare_digest(computed, signature_header)


def _get_webhook_secret() -> str | None:
    return get_secret("shopify_webhook_secret", required=False)


# ── Sender address (cached lazily) ────────────────────────────────────────────


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


# ── Draft persistence ─────────────────────────────────────────────────────────


def _append_draft(storage: Any, record: dict[str, Any]) -> None:
    try:
        line = json.dumps(record, ensure_ascii=False) + "\n"
        existing = b""
        try:
            buf = io.BytesIO()
            storage.download(_DRAFTS_KEY, buf)
            existing = buf.getvalue()
        except Exception:
            pass
        storage.upload(_DRAFTS_KEY, io.BytesIO(existing + line.encode()))
    except Exception as exc:
        logger.error("Failed to persist draft record for order %s: %s", record.get("shopify_order_id"), exc)


def _read_drafts(storage: Any) -> list[dict[str, Any]]:
    try:
        buf = io.BytesIO()
        storage.download(_DRAFTS_KEY, buf)
        lines = buf.getvalue().decode().splitlines()
        records = [json.loads(l) for l in lines if l.strip()]
        return sorted(records, key=lambda r: r.get("created_at", ""), reverse=True)
    except Exception:
        return []


# ── Background task: create draft ─────────────────────────────────────────────


def _create_draft(order: dict[str, Any], storage: Any) -> None:
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
    record: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "shopify_order_id": order_id,
        "shopify_order_number": str(order_number),
        "customer_name": customer_name,
        "courier": courier,
        "service": None,
        "tracking_number": None,
        "courier_draft_id": None,
        "status": "error",
        "shipping_address": {
            "street": f"{shipping_addr.get('address1', '')} {shipping_addr.get('address2', '')}".strip(),
            "city": shipping_addr.get("city", ""),
            "post_code": shipping_addr.get("zip", ""),
        },
        "parcel": {"template": "small", "weight_kg": None},
        "error": None,
    }

    try:
        sender = _get_sender()
        reference = str(order_number)

        if courier == "inpost":
            from zdrovena.common.inpost import InPostClient
            from zdrovena.common.secrets import get_secret

            token = get_secret("inpost_api_token")
            org_id = get_secret("inpost_organization_id")
            client = InPostClient(token, org_id)

            inpost_service = _pick_inpost_service(title)
            record["service"] = (
                "inpost_locker_standard" if inpost_service == "paczkomat" else "inpost_courier_standard"
            )

            if inpost_service == "paczkomat":
                # locker ID: try note_attributes, then parse from title
                note_attrs = {a["name"]: a["value"] for a in (order.get("note_attributes") or [])}
                locker_id = (
                    note_attrs.get("inpost_locker_id")
                    or note_attrs.get("paczkomat_id")
                    or note_attrs.get("locker_id")
                    or shipping_addr.get("address2", "")
                )
                result = client.create_paczkomat_shipment(
                    receiver_first_name=first_name,
                    receiver_last_name=last_name,
                    receiver_email=email,
                    receiver_phone=phone,
                    target_point=locker_id,
                    reference=reference,
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
                    reference=reference,
                )
                # dispatch order for kurier
                try:
                    client.create_dispatch_order(str(result["id"]), sender)
                except Exception as exc:
                    logger.warning("InPost dispatch order failed for %s: %s", reference, exc)

            record["courier_draft_id"] = str(result.get("id", ""))
            record["tracking_number"] = result.get("tracking_number")
            record["status"] = "created"
            record["parcel"]["template"] = "small"

        else:  # apaczka
            from zdrovena.common.apaczka import ApaczkaClient
            from zdrovena.common.secrets import get_secret

            app_id = get_secret("apaczka_app_id")
            app_secret = get_secret("apaczka_app_secret")
            service_id = get_secret("apaczka_service_id")
            client_a = ApaczkaClient(app_id, app_secret, service_id, storage)

            street = f"{shipping_addr.get('address1', '')} {shipping_addr.get('address2', '')}".strip()
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
                reference=reference,
            )
            record["courier_draft_id"] = str(result.get("id", ""))
            record["tracking_number"] = result.get("waybill_number")
            record["status"] = "created"
            record["service"] = "apaczka"
            record["parcel"]["weight_kg"] = 1.0

    except Exception as exc:
        logger.error(
            "Shipping draft creation failed for order %s (%s): %s",
            order_number, courier, exc
        )
        record["error"] = str(exc)

    _append_draft(storage, record)


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
    storage: StorageDep,
) -> dict[str, str]:
    raw_body = await request.body()

    sig_header = request.headers.get("X-Shopify-Hmac-Sha256", "")
    webhook_secret = _get_webhook_secret()

    if webhook_secret:
        if not sig_header:
            logger.warning("Shopify webhook received without HMAC header — rejected")
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing signature")
        if not _verify_shopify_hmac(raw_body, sig_header, webhook_secret):
            logger.warning("Shopify webhook HMAC mismatch — rejected")
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature")
    else:
        logger.warning("shopify-webhook-secret not configured — skipping HMAC validation")

    try:
        order = json.loads(raw_body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON")

    if not order.get("shipping_lines"):
        logger.warning("Order %s has no shipping_lines — skipping draft", order.get("id"))
        return {"status": "skipped"}

    background_tasks.add_task(_create_draft, order, storage)
    logger.info("Queued shipping draft for order %s", order.get("order_number") or order.get("id"))
    return {"status": "accepted"}


# ── Drafts list ───────────────────────────────────────────────────────────────


@router.get(
    "/shipping/drafts",
    summary="List shipping drafts",
    responses={403: {"description": "Insufficient role"}},
)
def list_drafts(
    storage: StorageDep,
    principal: Annotated[Principal, Depends(require_viewer_or_above)],
) -> dict[str, Any]:
    drafts = _read_drafts(storage)
    return {"drafts": drafts}


# ── Label streaming ───────────────────────────────────────────────────────────


@router.get(
    "/shipping/drafts/{draft_id}/label",
    summary="Stream shipping label PDF",
    responses={403: {"description": "Insufficient role"}, 404: {"description": "Draft not found"}},
)
def get_label(
    draft_id: str,
    storage: StorageDep,
    principal: Annotated[Principal, Depends(require_viewer_or_above)],
    courier: str = Query(..., description="inpost or apaczka"),
) -> StreamingResponse:
    drafts = _read_drafts(storage)
    draft = next((d for d in drafts if d["id"] == draft_id), None)
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
