"""zdrovena.api.routers.webhooks — Shopify webhooks + shipping drafts + label endpoints.

POST /webhooks/shopify/order-create          — Shopify order webhook (HMAC-validated)
GET  /shipping/drafts                         — list shipping drafts from Table Storage
GET  /shipping/drafts/{id}/label              — stream label PDF from courier
POST /shipping/drafts/{id}/execute            — (re)create courier shipment for a draft
POST /shipping/drafts/{id}/pickup             — order InPost kurier pickup
PATCH /shipping/drafts/{id}                   — update packages_count
DELETE /shipping/drafts/{id}/shipment         — cancel Ship-with-Allegro shipment
DELETE /shipping/drafts/{id}/dispatch         — cancel Ship-with-Allegro dispatch
DELETE /inpost/shipments/{id}                 — cancel InPost shipment before dispatch
DELETE /inpost/dispatch_orders/{id}           — cancel InPost dispatch order
DELETE /apaczka/orders/{id}                   — cancel Apaczka order
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Body,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import StreamingResponse

from zdrovena.api.auth import Principal, require_shipment_mgr_or_above, require_viewer_or_above
from zdrovena.api.deps import ShippingStoreDep, ShopifyDedupStoreDep, StorageDep
from zdrovena.audit.bottles import SKIP_RE, is_glass
from zdrovena.common.secrets import get_secret
from zdrovena.common.shipping_exceptions import (
    AllegroAuthError,
    AllegroBusinessError,
    AllegroCommandPending,
    CourierAuthError,
    CourierBusinessError,
    CourierTransientError,
    ZdrovenaShippingError,
)
from zdrovena.common.shipping_format import (
    extract_locker_id_from_title,
    normalize_pl_phone,
    parse_pl_address,
)
from zdrovena.common.shipping_store import ShippingStore
from zdrovena.common.shopify_dedup_store import DedupStoreError

logger = logging.getLogger("zdrovena.api.routers.webhooks")
_MOCK_COURIER = os.getenv("MOCK_COURIER", "").lower() in ("1", "true", "yes")

router = APIRouter(tags=["shipping"])


# ── HMAC helpers ──────────────────────────────────────────────────────────────


def _verify_shopify_hmac(raw_body: bytes, signature_header: str, secret: str) -> bool:
    computed = base64.b64encode(
        hmac.new(secret.encode(), raw_body, hashlib.sha256).digest()
    ).decode()
    return hmac.compare_digest(computed, signature_header)


def _get_webhook_secret() -> str | None:
    return get_secret("shopify_webhook_secret", required=False)


# Topics we actually process. A HMAC-valid payload from any other topic (e.g. a
# mis-configured products/create subscription) would crash _create_draft, so we
# reject unknown topics as defense-in-depth after HMAC.
ALLOWED_SHOPIFY_TOPICS = frozenset({"orders/create", "orders/updated"})


def _allowed_shopify_domains() -> frozenset[str] | None:
    """Whitelisted shop domains from SHOPIFY_ALLOWED_DOMAINS (comma-separated).

    Returns None when unset — dev mode, all domains accepted (with a warning).
    """
    raw = os.getenv("SHOPIFY_ALLOWED_DOMAINS", "").strip()
    if not raw:
        return None
    return frozenset(d.strip().lower() for d in raw.split(",") if d.strip())


def _is_shopify_topic_allowed(topic: str) -> bool:
    return topic in ALLOWED_SHOPIFY_TOPICS


def _is_shopify_domain_allowed(shop_domain: str) -> bool:
    allowed = _allowed_shopify_domains()
    if allowed is None:
        logger.warning(
            "SHOPIFY_ALLOWED_DOMAINS not configured — accepting webhook from %s (dev mode)",
            shop_domain or "<missing>",
        )
        return True
    return shop_domain.lower() in allowed


# ── Sender address ────────────────────────────────────────────────────────────


def _get_sender() -> dict[str, str]:
    _name = get_secret("sender_name", required=False) or ""
    return {
        "name": _name,
        "firstname": "",
        "lastname": _name,
        "street": get_secret("sender_street", required=False) or "",
        "building_number": get_secret("sender_building_number", required=False) or "1",
        "city": get_secret("sender_city", required=False) or "",
        "post_code": get_secret("sender_post_code", required=False) or "",
        "phone": get_secret("sender_phone", required=False) or "",
        "email": get_secret("sender_email", required=False) or "",
    }


# ── Address / phone parsing helpers ───────────────────────────────────────────


# ── SMS notification ─────────────────────────────────────────────────────────


def _maybe_send_new_order_sms(draft: dict[str, Any]) -> None:
    token = get_secret("smsapi_token", required=False)
    notify_phone = get_secret("notify_phone", required=False)
    if not token or not notify_phone:
        return
    try:
        from zdrovena.common.sms_service import send_new_order_sms

        send_new_order_sms(
            notify_phone=notify_phone,
            order_number=draft.get("shopify_order_number", ""),
            customer_name=draft.get("customer_name", ""),
            packages_count=draft.get("packages_count", 1),
            courier=draft.get("courier", ""),
            token=token,
        )
    except Exception as exc:
        logger.warning(
            "SMS notification failed for order %s: %s",
            draft.get("shopify_order_number"),
            exc,
        )


# ── Routing: decide courier from shipping_lines title ─────────────────────────


# ── Allegro helpers ───────────────────────────────────────────────────────────


def _allegro_carrier_id_for_courier(courier: str) -> str:
    """Map internal courier name to Allegro carrier code.

    Allegro's native carrier codes include INPOST, DPD, UPS, POCZTA, etc.
    Apaczka is a broker; when we ship through it we don't know the underlying
    carrier at the time of tracking-push, so we fall back to OTHER which
    accepts a free-text waybill.

    'allegro_delivery' (Wysyłam z Allegro): the waybill is synced server-side
    by Allegro itself, so no manual push is ever needed — the mapping is only
    a fallback if the guard in _maybe_push_tracking_to_allegro is bypassed.
    """
    return "INPOST" if courier == "inpost" else "OTHER"


def _get_allegro_client() -> Any | None:
    """Build an AllegroClient from Key Vault secrets. Returns None if missing."""
    import os

    client_id = get_secret("allegro-client-id", required=False)
    client_secret = get_secret("allegro-client-secret", required=False)
    refresh_token = get_secret("allegro-refresh-token", required=False)
    if not (client_id and client_secret and refresh_token):
        return None
    from zdrovena.common.allegro import AllegroClient

    return AllegroClient(
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=refresh_token,
        env=os.environ.get("ALLEGRO_ENV", "prod"),
    )


def _maybe_push_tracking_to_allegro(draft: dict[str, Any]) -> None:
    """After a shipment is created, push the waybill back to Allegro.

    No-op when the draft is not Allegro-sourced, has no tracking number,
    or no external_order_id. Errors are logged but never re-raised — the
    local draft is already saved and the operator can retry manually.

    Skipped entirely for courier='allegro_delivery' (Ship with Allegro),
    because the waybill is already known to Allegro server-side.
    """
    if draft.get("source") != "allegro":
        return
    if draft.get("courier") == "allegro_delivery":
        return
    tracking = draft.get("tracking_number")
    external_id = str(draft.get("external_order_id") or "")
    if not tracking or not external_id:
        return
    client = _get_allegro_client()
    if client is None:
        logger.warning(
            "Allegro credentials missing — cannot push tracking %s for order %s",
            tracking,
            external_id,
        )
        return
    carrier_id = _allegro_carrier_id_for_courier(draft.get("courier", ""))
    try:
        client.create_shipment(
            order_id=external_id,
            carrier_id=carrier_id,
            waybill=tracking,
        )
        logger.info(
            "Pushed tracking %s to Allegro order %s (%s)",
            tracking,
            external_id,
            carrier_id,
        )
    except Exception:
        logger.exception("Failed to push tracking to Allegro for order %s", external_id)


def _pick_courier(order: dict[str, Any]) -> str:
    """Route to InPost only on explicit 'inpost' or 'paczkomat' keywords. 'kurier' alone → Apaczka."""
    lines = order.get("shipping_lines") or []
    title = (lines[0].get("title", "") if lines else "").lower()
    if "inpost" in title or "paczkomat" in title:
        return "inpost"
    return "apaczka"


def _pick_inpost_service(title: str) -> str:
    return "paczkomat" if "paczkomat" in title.lower() else "kurier"


# ── Courier execution helpers ─────────────────────────────────────────────────


def _parcel_template(draft: dict[str, Any]) -> str:
    """Derive InPost paczkomat template from packages_breakdown (bug #4)."""
    from zdrovena.common.inpost import PARCEL_SPECS

    breakdown = draft.get("packages_breakdown") or []
    for box_type in ("3-pak", "szkło-2pak", "2-pak", "szkło", "1-pak", "pół-pak"):
        if any(b.get("type") == box_type for b in breakdown):
            tpl = PARCEL_SPECS.get(box_type, {}).get("paczkomat_template")
            return tpl if tpl else "large"
    return "large"


def _parcel_weight_and_dims(draft: dict[str, Any]) -> tuple[float, dict[str, float]]:
    """Derive total weight and largest-box dimensions from packages_breakdown (bug #5)."""
    from zdrovena.common.inpost import _DEFAULT_DIMS, PARCEL_SPECS

    breakdown = draft.get("packages_breakdown") or []
    total_weight = 0.0
    largest_dims: dict[str, float] = _DEFAULT_DIMS
    largest_volume = 0.0

    for box in breakdown:
        box_type = box.get("type", "")
        qty = box.get("qty", 1)
        spec = PARCEL_SPECS.get(box_type)
        if not spec:
            continue
        total_weight += spec["weight_kg"] * qty
        vol = spec["length"] * spec["width"] * spec["height"]
        if vol > largest_volume:
            largest_volume = vol
            largest_dims = spec

    return (total_weight if total_weight > 0 else 6.0), largest_dims


def _run_inpost(
    draft: dict[str, Any],
    sender: dict[str, str],
    *,
    pickup_date: str | None = None,
    pickup_from: str | None = None,
    pickup_to: str | None = None,
) -> dict[str, Any]:
    """Create or recreate InPost shipment from stored draft fields. Returns patch dict."""
    if _MOCK_COURIER:
        ref = draft.get("shopify_order_number", "mock")
        logger.info("MOCK_COURIER: skipping InPost API for order %s", ref)
        return {
            "courier_draft_id": f"mock-inpost-{ref}",
            "dispatch_order_id": f"mock-dispatch-{ref}",
            "tracking_number": f"MOCK{ref}0000000000",
            "status": "created",
            "pickup_ordered": False,
            "error": None,
        }

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

    pickup_ordered = False
    dispatch_order_id: str | None = None

    if inpost_service == "paczkomat":
        template = _parcel_template(draft)  # fix #4: correct locker size
        result = client.create_paczkomat_shipment(
            receiver_first_name=first_name,
            receiver_last_name=last_name,
            receiver_email=email,
            receiver_phone=phone,
            target_point=receiver.get("locker_id", ""),
            reference=reference,
            template=template,
        )
    else:
        addr = draft.get("shipping_address") or {}
        weight_kg, dims = _parcel_weight_and_dims(draft)  # fix #5: real dims from spec
        result = client.create_kurier_shipment(
            receiver_first_name=first_name,
            receiver_last_name=last_name,
            receiver_email=email,
            receiver_phone=phone,
            receiver_street=addr.get("street", ""),
            receiver_building_number=addr.get("building_number", "1"),  # fix #3
            receiver_city=addr.get("city", ""),
            receiver_post_code=addr.get("post_code", ""),
            sender=sender,
            reference=reference,
            weight_kg=weight_kg,
            dimensions=dims,
        )

    # Both paczkomat (drzwi→paczkomat) and kurier use dispatch_order for sender pickup
    try:
        dispatch_result = client.create_dispatch_order(
            str(result["id"]),
            sender,
            pickup_date=pickup_date,
            pickup_from=pickup_from,
            pickup_to=pickup_to,
        )
        dispatch_order_id = str(dispatch_result.get("id", "")) or None  # fix #6: save ID
        pickup_ordered = True
    except Exception as exc:
        logger.warning("InPost dispatch order failed for %s: %s", reference, exc)

    return {
        "courier_draft_id": str(result.get("id", "")),
        "dispatch_order_id": dispatch_order_id,  # fix #6
        "tracking_number": result.get("tracking_number"),
        "status": "created",
        "pickup_ordered": pickup_ordered,
        "error": None,
    }


def _run_apaczka(
    draft: dict[str, Any],
    sender: dict[str, str],
    storage: Any,
    *,
    pickup_date: str | None = None,
    pickup_from: str | None = None,
    pickup_to: str | None = None,
) -> dict[str, Any]:
    """Create or recreate Apaczka shipment from stored draft fields. Returns patch dict."""
    if _MOCK_COURIER:
        ref = draft.get("shopify_order_number", "mock")
        logger.info("MOCK_COURIER: skipping Apaczka API for order %s", ref)
        return {
            "courier_draft_id": f"mock-apaczka-{ref}",
            "tracking_number": f"APZ{ref}000000",
            "status": "created",
            "pickup_ordered": False,
            "error": None,
        }

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
        pickup_date=pickup_date,
        pickup_from=pickup_from,
        pickup_to=pickup_to,
    )
    return {
        "courier_draft_id": str(result.get("id", "")),
        "tracking_number": result.get("waybill_number"),
        "status": "created",
        "error": None,
    }


def _run_allegro_delivery(
    draft: dict[str, Any],
    storage: Any,
    *,
    pickup_date: str | None = None,
    pickup_from: str | None = None,
    pickup_to: str | None = None,
) -> dict[str, Any]:
    """Create shipment via Wysyłam z Allegro (Ship with Allegro).

    Flow:
      1. create-commands (POST) with delivery_method_id + optional credentials
      2. poll until SUCCESS → shipmentId
      3. GET shipment → extract carrierWaybill from packages.transportingInfo
      4. optionally order pickup via pickup-proposals + pickups/create-commands

    Draft fields consumed:
      allegro_delivery_method_id — required (from get_delivery_services)
      allegro_credentials_id     — None for Allegro Standard, string for own agreement
      allegro_sending_method     — InPost only: parcel_locker | dispatch_order | pop | any_point

    Errors bubble up to execute_draft which converts them to HTTP 502.
    """
    import uuid as _uuid

    if _MOCK_COURIER:
        ref = draft.get("shopify_order_number", "mock")
        logger.info("MOCK_COURIER: skipping Allegro Delivery API for order %s", ref)
        return {
            "courier_draft_id": f"mock-allegro-{ref}",
            "tracking_number": f"AWA{ref}00000",
            "status": "created",
            "pickup_ordered": False,
            "error": None,
        }

    # Duplicate guard: jeśli draft ma już otwartą komendę Allegro w stanie pending,
    # NIE tworzymy drugiej — zwracamy istniejący command_id żeby worker mógł dopytać.
    # Zapobiega podwójnej wysyłce, jeśli execute_draft zostanie zawołany drugi raz
    # zanim asynchroniczna komenda zakończy się po stronie Allegro.
    existing_cmd = draft.get("allegro_command_id")
    if existing_cmd and draft.get("status") == "pending_confirmation":
        logger.info(
            "Allegro command %s already pending for draft %s — skipping create",
            existing_cmd,
            draft.get("id"),
        )
        return {
            "courier_draft_id": None,
            "tracking_number": None,
            "status": "pending_confirmation",
            "pickup_ordered": False,
            "allegro_command_id": existing_cmd,
            "error": None,
        }

    client = _get_allegro_client()
    if client is None:
        raise RuntimeError("Allegro credentials missing — cannot use Ship with Allegro")

    order_id = str(draft.get("external_order_id") or "")
    delivery_method_id = draft.get("allegro_delivery_method_id")
    if not order_id or not delivery_method_id:
        raise RuntimeError(
            "Ship with Allegro requires external_order_id and allegro_delivery_method_id"
        )

    # Build packages per Allegro create-commands contract: FLAT dimensions, each a
    # {"value", "unit"} object; weight unit is the plural "KILOGRAMS"; type is required.
    weight_kg, dims = _parcel_weight_and_dims(draft)
    packages = [
        {
            "type": "PACKAGE",
            "length": {"value": dims["length"], "unit": "CENTIMETER"},
            "width": {"value": dims["width"], "unit": "CENTIMETER"},
            "height": {"value": dims["height"], "unit": "CENTIMETER"},
            "weight": {"value": round(weight_kg, 2), "unit": "KILOGRAMS"},
        }
    ]

    # sender/receiver blocks are required by the API. Pull them from the order's
    # delivery proposal (prefilled with the buyer's address by Allegro).
    proposal = client.get_delivery_proposal(order_id)
    sender = proposal.get("senderData") or {}
    receiver = dict(proposal.get("receiverData") or {})

    # Pickup-point / locker code lives inside the receiver block as `point`.
    pickup_point_id = (draft.get("receiver") or {}).get("locker_id") or None
    if pickup_point_id:
        receiver["point"] = pickup_point_id

    # TODO: map draft["allegro_sending_method"] to a valid Allegro additionalServices
    # string once the courier→service mapping is confirmed. The previous
    # "sendingAtPoint"/"parcel_locker" values were not valid API values, so we omit
    # additionalServices for now rather than send a 400-inducing payload.
    command_id = str(_uuid.uuid4())

    client.create_ship_with_allegro_shipment(
        command_id=command_id,
        order_id=order_id,
        delivery_method_id=delivery_method_id,
        credentials_id=draft.get("allegro_credentials_id"),
        packages=packages,
        sender=sender,
        receiver=receiver,
    )

    # Non-blocking: krótki polling ~3s. Jeśli create-command jeszcze IN_PROGRESS — zwracamy
    # status='pending_confirmation' i zostawiamy dopytanie o waybill oddzielnemu workerowi.
    # Unikamy trzymania HTTP requesta na kilkadziesiąt sekund (poprzedni problem z InPost sync).
    try:
        shipment_id = client.wait_for_ship_with_allegro_shipment(
            command_id, max_attempts=3, interval_s=1.0
        )
    except AllegroCommandPending as exc:
        # Osobny podtyp wyjątku — nie sprawdzamy substringu w message.
        logger.info(
            "Allegro Delivery create-command %s pending — draft %s -> pending_confirmation",
            exc.command_id or command_id,
            draft.get("id"),
        )
        return {
            "courier_draft_id": None,
            "tracking_number": None,
            "status": "pending_confirmation",
            "pickup_ordered": False,
            "allegro_command_id": exc.command_id or command_id,
            "error": None,
        }

    shipment = client.get_ship_with_allegro_shipment(shipment_id)
    _carrier_id, waybill = client.extract_shipment_waybill(shipment)

    pickup_ordered = False
    if pickup_date:
        try:
            proposals = client.get_ship_with_allegro_pickup_proposals([shipment_id])
            if proposals:
                pu_cmd = str(_uuid.uuid4())
                client.create_ship_with_allegro_pickup(
                    command_id=pu_cmd,
                    proposal_item_id=proposals[0].get("id"),
                    shipment_ids=[shipment_id],
                )
                pickup_ordered = True
            else:
                logger.warning(
                    "No pickup proposals available for shipment %s on %s",
                    shipment_id,
                    pickup_date,
                )
        except (AllegroBusinessError, AllegroAuthError, CourierTransientError):
            # Pickup is best-effort: the shipment is already created, so a pickup
            # failure must not abort the flow — operator can retry the pickup.
            logger.exception("Allegro Delivery pickup failed for %s", shipment_id)

    return {
        "courier_draft_id": shipment_id,
        "allegro_shipment_id": shipment_id,
        "tracking_number": waybill,
        "status": "created",
        "pickup_ordered": pickup_ordered,
        "allegro_command_id": command_id,
        "error": None,
    }


# ── Background task: create draft on Shopify webhook ─────────────────────────


def _calc_packages(
    product_items: list[dict[str, Any]],
) -> tuple[int, list[dict[str, Any]]]:
    """Return (packages_count, packages_breakdown) for a list of filtered line items.

    Plastik: greedy largest-box-first (3-pak → 2-pak → 1-pak → pół-pak).
    Szkło: greedy 2-pak consolidation (szkło-2pak → szkło for remainder).
    """
    plastic_qty = 0
    glass_qty = 0
    for item in product_items:
        qty = item.get("quantity", 1)
        if is_glass(item.get("name", "")):
            glass_qty += qty
        else:
            plastic_qty += qty

    breakdown: list[dict[str, Any]] = []

    # Plastik — greedy
    remaining = plastic_qty
    for box_size, label in ((3, "3-pak"), (2, "2-pak"), (1, "1-pak")):
        if remaining >= box_size:
            count = remaining // box_size
            breakdown.append({"type": label, "qty": count})
            remaining -= count * box_size
    if remaining > 0:
        breakdown.append({"type": "pół-pak", "qty": 1})

    # Szkło — greedy: 2-pak first, then single boxes
    remaining_glass = glass_qty
    if remaining_glass >= 2:
        count = remaining_glass // 2
        breakdown.append({"type": "szkło-2pak", "qty": count})
        remaining_glass -= count * 2
    if remaining_glass > 0:
        breakdown.append({"type": "szkło", "qty": remaining_glass})

    total = sum(b["qty"] for b in breakdown)
    return max(total, 1), breakdown


def _create_draft(
    order: dict[str, Any],
    shipping_store: ShippingStore,
    storage: Any,
    *,
    source: str = "shopify",
) -> None:
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

    # fix #2: locker_id from title first, then note_attributes fallbacks
    note_attrs = {a["name"]: a["value"] for a in (order.get("note_attributes") or [])}

    # Wysyłam z Allegro (Ship with Allegro): dla source='allegro' z AllegroDeliveryMethodId
    # całkowicie zastępujemy InPost/Apaczkę — przesyłkę tworzy Allegro po stronie serwera.
    allegro_method_id = (note_attrs.get("AllegroDeliveryMethodId") or "").strip()
    use_allegro_delivery = source == "allegro" and bool(allegro_method_id)

    if use_allegro_delivery:
        courier = "allegro_delivery"
        # sendingAtPoint tylko dla InPost — rozpoznajemy po nazwie w shipping_lines.title.
        # Paczkomat: 'parcel_locker' (jednoznaczne, bezpieczne).
        # InPost Kurier: brak defaultu — operator musi świadomie ustawić sending_method.
        # Za flagą ALLEGRO_INPOST_KURIER_DEFAULT (dispatch_order|pop|any_point) można
        # włączyć domyślne mapowanie.
        title_lower = title.lower()
        if "paczkomat" in title_lower:
            allegro_sending_method: str | None = "parcel_locker"
        elif "inpost" in title_lower:
            kurier_default = (os.getenv("ALLEGRO_INPOST_KURIER_DEFAULT") or "").strip()
            allegro_sending_method = kurier_default or None
        else:
            allegro_sending_method = None
        inpost_service = "paczkomat" if allegro_sending_method == "parcel_locker" else None
    else:
        courier = _pick_courier(order)
        inpost_service = _pick_inpost_service(title) if courier == "inpost" else None
        allegro_sending_method = None

    line_items = order.get("line_items") or []
    product_items = [item for item in line_items if not SKIP_RE.search(item.get("name", ""))]
    total_qty = max(sum(item.get("quantity", 1) for item in product_items), 1)
    packages_count, packages_breakdown = _calc_packages(product_items)
    if inpost_service == "paczkomat":
        locker_id = (
            extract_locker_id_from_title(title)
            or note_attrs.get("PickupPointId")
            or note_attrs.get("inpost_locker_id")
            or note_attrs.get("paczkomat_id")
            or note_attrs.get("locker_id")
            or shipping_addr.get("address2", "")
        )
    else:
        locker_id = ""

    if courier == "allegro_delivery":
        service = "allegro_delivery"
    elif courier == "inpost":
        service = (
            "inpost_locker_standard" if inpost_service == "paczkomat" else "inpost_courier_standard"
        )
    else:
        service = "apaczka"

    # fix #3: parse address1 into street + building_number
    raw_address1 = shipping_addr.get("address1", "")
    street, building_number = parse_pl_address(raw_address1)

    # fix: normalize phone
    phone = normalize_pl_phone(phone) if phone else phone

    record: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "external_order_id": order_id,
        "shopify_order_id": order_id if source == "shopify" else None,
        "shopify_order_number": str(order_number),
        "customer_name": customer_name,
        "courier": courier,
        "service": service,
        "tracking_number": None,
        "courier_draft_id": None,
        "dispatch_order_id": None,  # fix #6: field exists from creation
        "status": "needs_review" if (packages_count > 1 or phone is None) else "pending",
        "packages_count": packages_count,
        "packages_breakdown": packages_breakdown,
        "total_qty": total_qty,
        "order_items": [
            {"name": item.get("name") or item.get("title", ""), "quantity": item.get("quantity", 1)}
            for item in product_items
        ],
        "pickup_ordered": False,
        "receiver": {
            "first_name": first_name,
            "last_name": last_name,
            "email": email,
            "phone": phone,
            "locker_id": locker_id,
        },
        "shipping_address": {
            "street": street,
            "building_number": building_number,  # fix #3
            "city": shipping_addr.get("city", ""),
            "post_code": shipping_addr.get("zip", ""),
        },
        "parcel": {"template": "large", "weight_kg": None},  # fix #4: large is safe default
        "error": None,
    }

    # Wysyłam z Allegro — dodatkowe pola potrzebne dla /shipment-management/*
    if courier == "allegro_delivery":
        record["allegro_delivery_method_id"] = allegro_method_id
        record["allegro_credentials_id"] = None  # Allegro Standard; nadpisze się dla własnej umowy
        record["allegro_sending_method"] = allegro_sending_method

    shipping_store.upsert_draft(record)
    _maybe_send_new_order_sms(record)


# ── Webhook endpoint ──────────────────────────────────────────────────────────


@router.post(
    "/webhooks/shopify/order-create",
    status_code=status.HTTP_200_OK,
    summary="Shopify order webhook — creates shipping draft",
    include_in_schema=False,
)
async def shopify_order_created(
    request: Request,
    background_tasks: BackgroundTasks,
    shipping_store: ShippingStoreDep,
    storage: StorageDep,
    dedup_store: ShopifyDedupStoreDep,
) -> dict[str, str]:
    raw_body = await request.body()

    sig_header = request.headers.get("X-Shopify-Hmac-Sha256", "")
    webhook_id = request.headers.get("X-Shopify-Webhook-Id", "")
    topic = request.headers.get("X-Shopify-Topic", "")
    shop_domain = request.headers.get("X-Shopify-Shop-Domain", "")
    webhook_secret = _get_webhook_secret()

    # 1. HMAC — always required. There is no unsigned bypass: an unsigned payload
    #    could forge orders and ship parcels to arbitrary addresses.
    if not webhook_secret:
        logger.warning("shopify-webhook-secret not configured — rejecting webhook")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Webhook secret not configured",
        )
    if not sig_header:
        logger.warning("Shopify webhook received without HMAC header — rejected")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing signature")
    if not _verify_shopify_hmac(raw_body, sig_header, webhook_secret):
        logger.warning("Shopify webhook HMAC mismatch — rejected")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature")

    # 2. Whitelist topic + shop domain (defense-in-depth after HMAC).
    if not _is_shopify_topic_allowed(topic):
        logger.warning("Shopify webhook with disallowed topic %r (id=%s) — rejected", topic, webhook_id)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Topic not allowed")
    if not _is_shopify_domain_allowed(shop_domain):
        logger.warning("Shopify webhook from disallowed shop %r (id=%s) — rejected", shop_domain, webhook_id)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Shop domain not allowed")

    # 3. Parse the (now trusted) body.
    try:
        order = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON") from exc

    # 4. Deduplicate by X-Shopify-Webhook-Id. Save the id BEFORE processing so a
    #    retry of the same delivery is a no-op. Fail-closed (503) if the dedup store
    #    is unavailable so Shopify retries rather than us risking a duplicate draft.
    if webhook_id:
        try:
            if dedup_store.is_duplicate(webhook_id):
                logger.info("Duplicate Shopify webhook %s — skipping", webhook_id)
                return {"status": "duplicate", "webhook_id": webhook_id}
            dedup_store.mark_seen(webhook_id)
        except DedupStoreError:
            logger.exception("Shopify dedup store unavailable for webhook %s", webhook_id)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Dedup store unavailable",
            ) from None
    else:
        logger.warning("Shopify webhook missing X-Shopify-Webhook-Id — dedup skipped")

    # 5. Orders without shipping lines never become drafts.
    if not order.get("shipping_lines"):
        logger.warning("Order %s has no shipping_lines — skipping draft", order.get("id"))
        return {"status": "skipped"}

    # 6. Heavy work off the request path (Shopify enforces a 5s timeout).
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
    pickup_date: str | None = Body(None),
    pickup_from: str | None = Body(None),
    pickup_to: str | None = Body(None),
) -> dict[str, Any]:
    draft = shipping_store.get_draft(draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")
    if draft.get("status") == "needs_review":
        raise HTTPException(
            status_code=409,
            detail="Draft requires review (multi-package) — use PATCH to override",
        )
    if draft.get("status") == "created":
        raise HTTPException(
            status_code=409,
            detail="Draft already executed — use pickup endpoint to order collection",
        )

    pickup_schedule = {
        "pickup_date": pickup_date,
        "pickup_from": pickup_from,
        "pickup_to": pickup_to,
    }

    try:
        sender = _get_sender()
        courier = draft.get("courier", "apaczka")
        if courier == "allegro_delivery":
            patch = _run_allegro_delivery(draft, storage, **pickup_schedule)
        elif courier == "inpost":
            patch = _run_inpost(draft, sender, **pickup_schedule)
        else:
            patch = _run_apaczka(draft, sender, storage, **pickup_schedule)
    except Exception as exc:
        logger.exception("execute_draft failed for %s", draft_id)
        shipping_store.update_draft(draft_id, {"status": "error", "error": str(exc)})
        raise HTTPException(status_code=502, detail=f"Courier API error: {exc}") from exc

    shipping_store.update_draft(draft_id, patch)
    updated = shipping_store.get_draft(draft_id)
    if updated:
        _maybe_push_tracking_to_allegro(updated)
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
    pickup_date: str | None = Body(None),
    pickup_from: str | None = Body(None),
    pickup_to: str | None = Body(None),
) -> dict[str, Any]:
    draft = shipping_store.get_draft(draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")
    if draft.get("courier") != "inpost":
        raise HTTPException(status_code=400, detail="Pickup only available for InPost shipments")
    if draft.get("status") != "created":
        raise HTTPException(status_code=409, detail="Draft must be in 'created' state")
    if draft.get("pickup_ordered"):
        raise HTTPException(status_code=409, detail="Pickup already ordered")

    courier_draft_id = draft.get("courier_draft_id")
    if not courier_draft_id:
        raise HTTPException(status_code=409, detail="No courier draft ID — execute first")

    if _MOCK_COURIER:
        ref = draft.get("shopify_order_number", "mock")
        logger.info("MOCK_COURIER: skipping InPost dispatch order for draft %s", ref)
    else:
        try:
            from zdrovena.common.inpost import InPostClient

            token = get_secret("inpost_api_token")
            org_id = get_secret("inpost_organization_id")
            client = InPostClient(token, org_id)
            sender = _get_sender()
            client.create_dispatch_order(
                courier_draft_id,
                sender,
                pickup_date=pickup_date,
                pickup_from=pickup_from,
                pickup_to=pickup_to,
            )
        except Exception as exc:
            logger.exception("order_pickup failed for draft %s", draft_id)
            raise HTTPException(status_code=502, detail=f"InPost dispatch error: {exc}") from exc

    shipping_store.update_draft(draft_id, {"pickup_ordered": True})
    return {"status": "pickup_ordered", "draft_id": draft_id}


# ── Cancel (Ship with Allegro) ────────────────────────────────────────────────


@router.delete(
    "/shipping/drafts/{draft_id}/shipment",
    summary="Cancel a Ship-with-Allegro shipment before dispatch",
    responses={
        403: {"description": "Insufficient role"},
        404: {"description": "Draft not found"},
        409: {"description": "No Allegro shipment to cancel"},
        502: {"description": "Allegro API error"},
    },
)
def cancel_shipment(
    draft_id: str,
    shipping_store: ShippingStoreDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
) -> dict[str, Any]:
    """Cancel the Allegro shipment created for this draft (before it is dispatched)."""
    draft = shipping_store.get_draft(draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")

    shipment_id = draft.get("allegro_shipment_id") or draft.get("courier_draft_id")
    if not shipment_id:
        raise HTTPException(status_code=409, detail="No Allegro shipment to cancel")

    if not _MOCK_COURIER:
        client = _get_allegro_client()
        if client is None:
            raise HTTPException(status_code=502, detail="Allegro credentials missing")
        try:
            client.cancel_ship_with_allegro_shipment(
                command_id=str(uuid.uuid4()), shipment_id=str(shipment_id)
            )
        except (AllegroBusinessError, AllegroAuthError, CourierTransientError) as exc:
            logger.exception("Allegro cancel shipment failed for draft %s", draft_id)
            raise HTTPException(status_code=502, detail=f"Allegro API error: {exc}") from exc

    shipping_store.update_draft(
        draft_id, {"status": "cancelled", "allegro_shipment_id": None}
    )
    return {"status": "cancelled", "draft_id": draft_id, "shipment_id": str(shipment_id)}


@router.delete(
    "/shipping/drafts/{draft_id}/dispatch",
    summary="Cancel a Ship-with-Allegro dispatch (pickup) before acceptance",
    responses={
        403: {"description": "Insufficient role"},
        404: {"description": "Draft not found"},
        409: {"description": "No Allegro dispatch to cancel"},
        502: {"description": "Allegro API error"},
    },
)
def cancel_dispatch(
    draft_id: str,
    shipping_store: ShippingStoreDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
) -> dict[str, Any]:
    """Cancel the Allegro dispatch (pickup) order created for this draft."""
    draft = shipping_store.get_draft(draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")

    dispatch_id = draft.get("allegro_dispatch_id")
    if not dispatch_id:
        raise HTTPException(status_code=409, detail="No Allegro dispatch to cancel")

    if not _MOCK_COURIER:
        client = _get_allegro_client()
        if client is None:
            raise HTTPException(status_code=502, detail="Allegro credentials missing")
        try:
            client.cancel_ship_with_allegro_dispatch(
                command_id=str(uuid.uuid4()), dispatch_id=str(dispatch_id)
            )
        except (AllegroBusinessError, AllegroAuthError, CourierTransientError) as exc:
            logger.exception("Allegro cancel dispatch failed for draft %s", draft_id)
            raise HTTPException(status_code=502, detail=f"Allegro API error: {exc}") from exc

    shipping_store.update_draft(
        draft_id, {"pickup_ordered": False, "allegro_dispatch_id": None}
    )
    return {"status": "dispatch_cancelled", "draft_id": draft_id, "dispatch_id": str(dispatch_id)}


# ── Cancel (raw courier id: InPost / Apaczka) ─────────────────────────────────


def _courier_cancel_http_status(exc: ZdrovenaShippingError) -> int:
    """Map a shipping-hierarchy error onto an HTTP status for cancel endpoints.

    Auth -> 401, business (e.g. already dispatched / not cancellable) -> 409,
    transient (network/5xx) -> 503, anything else in the hierarchy -> 500.
    """
    if isinstance(exc, CourierAuthError):
        return status.HTTP_401_UNAUTHORIZED
    if isinstance(exc, CourierBusinessError):
        return status.HTTP_409_CONFLICT
    if isinstance(exc, CourierTransientError):
        return status.HTTP_503_SERVICE_UNAVAILABLE
    return status.HTTP_500_INTERNAL_SERVER_ERROR


def _build_inpost_client() -> Any:
    from zdrovena.common.inpost import InPostClient

    token = get_secret("inpost_api_token")
    org_id = get_secret("inpost_organization_id")
    return InPostClient(token, org_id)


@router.delete(
    "/inpost/shipments/{shipment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Cancel an InPost shipment before dispatch",
    responses={
        403: {"description": "Insufficient role"},
        409: {"description": "Shipment cannot be cancelled (already dispatched / unknown)"},
        503: {"description": "InPost API transient error"},
    },
)
def cancel_inpost_shipment(
    shipment_id: str,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
) -> Response:
    if _MOCK_COURIER:
        logger.info("MOCK_COURIER: skipping InPost cancel_shipment for %s", shipment_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    try:
        _build_inpost_client().cancel_shipment(shipment_id)
    except ZdrovenaShippingError as exc:
        logger.exception("InPost cancel_shipment failed for %s", shipment_id)
        raise HTTPException(
            status_code=_courier_cancel_http_status(exc), detail=f"InPost cancel error: {exc}"
        ) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/inpost/dispatch_orders/{dispatch_order_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Cancel an InPost dispatch order before courier acceptance",
    responses={
        403: {"description": "Insufficient role"},
        409: {"description": "Dispatch cannot be cancelled (already accepted / unknown)"},
        503: {"description": "InPost API transient error"},
    },
)
def cancel_inpost_dispatch(
    dispatch_order_id: str,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
) -> Response:
    if _MOCK_COURIER:
        logger.info("MOCK_COURIER: skipping InPost cancel_dispatch_order for %s", dispatch_order_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    try:
        _build_inpost_client().cancel_dispatch_order(dispatch_order_id)
    except ZdrovenaShippingError as exc:
        logger.exception("InPost cancel_dispatch_order failed for %s", dispatch_order_id)
        raise HTTPException(
            status_code=_courier_cancel_http_status(exc), detail=f"InPost cancel error: {exc}"
        ) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/apaczka/orders/{order_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Cancel an Apaczka order",
    responses={
        403: {"description": "Insufficient role"},
        409: {"description": "Order cannot be cancelled (already sent / unknown)"},
        503: {"description": "Apaczka API transient error"},
    },
)
def cancel_apaczka_order(
    order_id: str,
    storage: StorageDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
) -> Response:
    if _MOCK_COURIER:
        logger.info("MOCK_COURIER: skipping Apaczka cancel for %s", order_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    from zdrovena.common.apaczka import ApaczkaClient

    app_id = get_secret("apaczka_app_id")
    app_secret = get_secret("apaczka_app_secret")
    service_id = get_secret("apaczka_service_id")
    client = ApaczkaClient(app_id, app_secret, service_id, storage)
    try:
        client.cancel_shipment(order_id)
    except ZdrovenaShippingError as exc:
        logger.exception("Apaczka cancel failed for order %s", order_id)
        raise HTTPException(
            status_code=_courier_cancel_http_status(exc), detail=f"Apaczka cancel error: {exc}"
        ) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── Update packages_count ─────────────────────────────────────────────────────


@router.patch(
    "/shipping/drafts/{draft_id}",
    summary="Update draft metadata (packages_count, service, locker_id)",
    responses={
        403: {"description": "Insufficient role"},
        404: {"description": "Draft not found"},
        400: {"description": "Invalid service for courier"},
    },
)
def update_draft(
    draft_id: str,
    shipping_store: ShippingStoreDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
    packages_count: int | None = Body(None, ge=1, le=99),
    service: str | None = Body(None),
    locker_id: str | None = Body(None),
    reviewed: bool | None = Body(None),
) -> dict[str, Any]:
    draft = shipping_store.get_draft(draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")

    patch: dict[str, Any] = {}
    if packages_count is not None:
        patch["packages_count"] = packages_count
    if service is not None:
        valid = {"inpost_locker_standard", "inpost_courier_standard", "apaczka"}
        if service not in valid:
            raise HTTPException(status_code=400, detail=f"Unknown service: {service}")
        if draft.get("courier") == "inpost" and service == "apaczka":
            raise HTTPException(status_code=400, detail="Cannot switch InPost draft to apaczka")
        patch["service"] = service
    if locker_id is not None:
        receiver = dict(draft.get("receiver") or {})
        receiver["locker_id"] = locker_id
        patch["receiver"] = receiver
    if reviewed is True and draft.get("status") == "needs_review":
        patch["status"] = "pending"
        patch["error"] = None

    if patch:
        shipping_store.update_draft(draft_id, patch)
    updated = shipping_store.get_draft(draft_id)
    return updated or {"draft_id": draft_id}


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
    courier: str = Query(None, description="inpost or apaczka (defaults to draft's courier)"),
) -> StreamingResponse:
    draft = shipping_store.get_draft(draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")

    courier_draft_id = draft.get("courier_draft_id")
    if not courier_draft_id:
        raise HTTPException(status_code=404, detail="No courier draft ID — draft may have failed")

    # Prefer the stored draft courier over the query param (prevents mismatch)
    courier = draft.get("courier") or courier
    if courier not in ("inpost", "apaczka"):
        raise HTTPException(status_code=400, detail="courier must be 'inpost' or 'apaczka'")

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
        logger.exception("Label fetch failed for draft %s", draft_id)
        raise HTTPException(status_code=502, detail=f"Courier API error: {exc}") from exc

    order_num = draft.get("shopify_order_number", draft_id).lstrip("#")
    filename = f"label_{courier}_{order_num}.pdf"
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )
