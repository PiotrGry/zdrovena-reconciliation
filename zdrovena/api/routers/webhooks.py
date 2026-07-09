"""zdrovena.api.routers.webhooks — Shopify webhooks + shipping drafts + label endpoints.

POST /webhooks/shopify/order-create          — Shopify order webhook (HMAC-validated)
POST /webhooks/shopify/order-created         — legacy alias for order-create (compat)
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
from functools import lru_cache
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
from fastapi.responses import JSONResponse, StreamingResponse

from zdrovena.api.auth import Principal, require_shipment_mgr_or_above, require_viewer_or_above
from zdrovena.api.deps import ShippingStoreDep, ShopifyDedupStoreDep, StorageDep
from zdrovena.audit.bottles import SKIP_RE, is_glass
from zdrovena.common.secrets import get_secret
from zdrovena.common.shipping_exceptions import (
    AllegroAuthError,
    AllegroBusinessError,
    AllegroCommandPending,
    ApaczkaBusinessError,
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
    if not hmac.compare_digest(computed, signature_header):
        # Log truncated details to speed up local HMAC debugging without
        # leaking the full secret or signature to production logs.
        logger.warning(
            "HMAC mismatch: computed=%s... received=%s... body_len=%d",
            computed[:16],
            signature_header[:16],
            len(raw_body),
        )
        return False
    return True


def _get_webhook_secret() -> str | None:
    return get_secret("shopify_webhook_secret", required=False)


# Topics we actually process. A HMAC-valid payload from any other topic (e.g. a
# mis-configured products/create subscription) would crash _create_draft, so we
# reject unknown topics as defense-in-depth after HMAC.
# NOTE: only `orders/create` is accepted today. `orders/updated` was previously
# whitelisted, but the current handler creates a shipping draft — firing that on
# every order update would produce unwanted duplicate drafts. Once we have a
# dedicated update-handler with clear semantics we can re-add `orders/updated`.
ALLOWED_SHOPIFY_TOPICS = frozenset({"orders/create"})


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


def _is_production_env() -> bool:
    """True when APP_ENV / DEPLOY_ENV / AZURE_ENV signals a production deploy.

    We treat *any* of {production, prod, live} as production. Development,
    sandbox, staging, and unset values are non-production. Case-insensitive.
    """
    for var in ("APP_ENV", "DEPLOY_ENV", "AZURE_ENV", "ENV"):
        value = os.environ.get(var, "").strip().lower()
        if value in {"production", "prod", "live"}:
            return True
    return False


def _is_shopify_domain_allowed(shop_domain: str) -> bool:
    """Return True when the shop domain is on the SHOPIFY_ALLOWED_DOMAINS whitelist.

    Fail-closed policy:
      * SHOPIFY_ALLOWED_DOMAINS unset in a **production** environment is a
        misconfiguration — we reject the webhook rather than silently accept
        every caller. Production is detected via APP_ENV/DEPLOY_ENV/AZURE_ENV/ENV
        being one of {production, prod, live}.
      * SHOPIFY_ALLOWED_DOMAINS unset in dev/sandbox/staging keeps the previous
        permissive behaviour (with a warning) so local development doesn't
        require boilerplate config.
    """
    allowed = _allowed_shopify_domains()
    if allowed is None:
        if _is_production_env():
            logger.error(
                "SHOPIFY_ALLOWED_DOMAINS is not configured in production — "
                "rejecting webhook from %s",
                shop_domain or "<missing>",
            )
            return False
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
    """Build an AllegroClient from Key Vault secrets. Returns None if missing.

    Uses ``SecretsAllegroTokenStore`` so rotated refresh tokens are persisted
    back to Key Vault / keyring — without this, the first restart after a
    rotation would break the integration (Allegro rotates refresh tokens on
    every use).
    """
    import os

    client_id = get_secret("allegro-client-id", required=False)
    client_secret = get_secret("allegro-client-secret", required=False)
    refresh_token = get_secret("allegro-refresh-token", required=False)
    if not (client_id and client_secret and refresh_token):
        return None
    from zdrovena.common.allegro import AllegroClient, SecretsAllegroTokenStore

    return AllegroClient(
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=refresh_token,
        env=os.environ.get("ALLEGRO_ENV", "prod"),
        token_store=SecretsAllegroTokenStore(),
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


def _parse_title_map(raw: str) -> dict[str, str]:
    """Parse env-var mapping in JSON or ``keyword=value;keyword=value`` format.

    Keys are lowercased and stripped. Empty/invalid entries are ignored.
    Returns an empty dict for empty input or parse failure.
    """
    if not raw or not raw.strip():
        return {}
    text = raw.strip()
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("Failed to parse title map as JSON, ignoring: %r", raw)
            return {}
        if not isinstance(parsed, dict):
            return {}
        return {
            str(k).strip().lower(): str(v).strip()
            for k, v in parsed.items()
            if str(k).strip() and str(v).strip()
        }
    result: dict[str, str] = {}
    # accept both ';' and ',' as pair separators for operator convenience
    for chunk in text.replace(",", ";").split(";"):
        if "=" not in chunk:
            continue
        key, _, value = chunk.partition("=")
        key = key.strip().lower()
        value = value.strip()
        if key and value:
            result[key] = value
    return result


@lru_cache(maxsize=1)
def _courier_title_map() -> dict[str, str]:
    """Explicit shipping-line title → courier mapping from ``COURIER_TITLE_MAP``.

    Example: ``COURIER_TITLE_MAP="inpost=inpost;paczkomat=inpost;dpd=apaczka"``.
    Empty map preserves the substring-heuristic fallback.
    """
    return _parse_title_map(os.getenv("COURIER_TITLE_MAP", ""))


@lru_cache(maxsize=1)
def _inpost_service_title_map() -> dict[str, str]:
    """Explicit title → InPost service mapping from ``INPOST_SERVICE_TITLE_MAP``.

    Example: ``INPOST_SERVICE_TITLE_MAP="paczkomat=paczkomat;kurier=kurier"``.
    """
    return _parse_title_map(os.getenv("INPOST_SERVICE_TITLE_MAP", ""))


@lru_cache(maxsize=1)
def _apaczka_service_title_map() -> dict[str, str]:
    """Explicit title → Apaczka service_id mapping from ``APACZKA_SERVICE_TITLE_MAP``.

    Example: ``APACZKA_SERVICE_TITLE_MAP="dpd kurier=21;orlen paczka=53"``.
    Values not present in APACZKA_SERVICE_CATALOG are dropped (logged as a
    warning) rather than silently reaching a live Apaczka shipment — an
    operator misconfiguration then falls back to the same needs_review path
    already used when no mapping is configured at all, instead of shipping
    through a wrong/unintended courier channel.
    """
    from zdrovena.common.apaczka import APACZKA_SERVICE_CATALOG

    raw_map = _parse_title_map(os.getenv("APACZKA_SERVICE_TITLE_MAP", ""))
    valid_map: dict[str, str] = {}
    for keyword, service_id in raw_map.items():
        if service_id in APACZKA_SERVICE_CATALOG:
            valid_map[keyword] = service_id
        else:
            logger.warning(
                "APACZKA_SERVICE_TITLE_MAP: keyword %r maps to unknown "
                "service_id %r (not in APACZKA_SERVICE_CATALOG) — ignoring, "
                "titles matching this keyword will route to needs_review",
                keyword,
                service_id,
            )
    return valid_map


def _reset_courier_maps_cache() -> None:
    """Clear cached ENV mapping (test-only helper)."""
    _courier_title_map.cache_clear()
    _inpost_service_title_map.cache_clear()
    _apaczka_service_title_map.cache_clear()


def _pick_courier(order: dict[str, Any]) -> str:
    """Route shipping-line title to a courier backend.

    Consults ``COURIER_TITLE_MAP`` env var first (explicit keyword→courier pairs);
    falls back to the substring heuristic (``inpost``/``paczkomat`` → inpost,
    otherwise apaczka) for backwards compatibility.
    """
    lines = order.get("shipping_lines") or []
    title = (lines[0].get("title", "") if lines else "").lower()
    explicit = _courier_title_map()
    if explicit:
        for keyword, courier in explicit.items():
            if keyword and keyword in title:
                return courier
    if "inpost" in title or "paczkomat" in title:
        return "inpost"
    return "apaczka"


def _pick_inpost_service(title: str) -> str:
    """Pick InPost service (``paczkomat``/``kurier``) from shipping-line title.

    Consults ``INPOST_SERVICE_TITLE_MAP`` first; falls back to substring match.
    """
    lowered = title.lower()
    explicit = _inpost_service_title_map()
    if explicit:
        for keyword, service in explicit.items():
            if keyword and keyword in lowered:
                return service
    return "paczkomat" if "paczkomat" in lowered else "kurier"


def _pick_apaczka_service(title: str) -> str | None:
    """Pick an Apaczka service_id from shipping-line title.

    Unlike ``_pick_courier``/``_pick_inpost_service`` there is no
    substring-heuristic fallback: Apaczka's title strings are
    business-configured Shopify shipping-method names, not consistently
    predictable substrings. No configured match -> None; the caller treats
    that as needing manual review before shipping, rather than guessing
    which of Apaczka's ~70 courier products to use.
    """
    lowered = title.lower()
    for keyword, service_id in _apaczka_service_title_map().items():
        if keyword and keyword in lowered:
            return service_id
    return None


# ── Courier execution helpers ─────────────────────────────────────────────────


def _parcel_template(draft: dict[str, Any]) -> str:
    """Derive InPost paczkomat template from packages_breakdown.

    Preferred path (P2-1): compute total weight + largest package dims and let
    ``pick_paczkomat_template`` choose the smallest fitting slot (cheaper for
    orders that would fit in an A/B slot). Falls back to the largest box's
    static ``paczkomat_template`` and finally to ``"large"`` for safety.
    """
    from zdrovena.common.inpost import PARCEL_SPECS, pick_paczkomat_template

    breakdown = draft.get("packages_breakdown") or []

    # 1. auto-pick by dims + weight of the largest box (safest single-parcel pick)
    total_weight, largest_dims = _parcel_weight_and_dims(draft)
    if breakdown and largest_dims:
        auto = pick_paczkomat_template(dict(largest_dims), total_weight)
        if auto:
            return auto

    # 2. static fallback — largest box in the breakdown
    for box_type in ("3-pak", "szkło-2pak", "2-pak", "szkło", "1-pak", "pół-pak"):
        if any(b.get("type") == box_type for b in breakdown):
            tpl = PARCEL_SPECS.get(box_type, {}).get("paczkomat_template")
            return tpl if tpl else "large"

    # 3. no breakdown — default to the biggest slot (guaranteed acceptance)
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
    service_id = draft.get("apaczka_service_id") or ""
    if not service_id:
        # Guard against silently sending an empty service_id to Apaczka's live,
        # paid create_shipment API. A None/missing apaczka_service_id means the
        # draft was never matched against the Shopify shipping-line title map
        # (see _pick_apaczka_service) and should have stayed in needs_review —
        # raising here turns a would-be-silent bad shipment into a loud,
        # visible error (caught by execute_draft's except Exception handler,
        # which marks the draft status="error" and returns HTTP 502).
        raise ApaczkaBusinessError(
            f"Draft {draft.get('id')} has no apaczka_service_id — cannot create shipment",
            order_id=str(draft.get("id", "")),
            courier="apaczka",
            action="create_shipment",
        )
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
    if not order_id:
        raise RuntimeError("Ship with Allegro requires external_order_id")
    # deliveryMethodId is optional since 2026-07-01 — Allegro auto-derives it
    # from the order. Kept read here so callers with own agreements can still
    # force a specific method by populating allegro_delivery_method_id in the
    # draft, but its absence must NOT abort the flow.
    delivery_method_id = draft.get("allegro_delivery_method_id") or None

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

    # Map InPost sending mode to Allegro additionalProperties.inpost#sendingMethod.
    # Contract per Allegro issue #9915 (https://github.com/allegro/allegro-api/issues/9915):
    # valid enum values are parcel_locker | dispatch_order | pop | any_point.
    # Only sent for InPost draft s; other carriers derive the field from the order.
    _ALLEGRO_INPOST_SENDING_METHODS = {
        "parcel_locker",
        "dispatch_order",
        "pop",
        "any_point",
    }
    additional_properties: dict[str, Any] | None = None
    sending_method = draft.get("allegro_sending_method")
    if sending_method and sending_method in _ALLEGRO_INPOST_SENDING_METHODS:
        additional_properties = {"inpost#sendingMethod": sending_method}

    command_id = str(_uuid.uuid4())

    client.create_ship_with_allegro_shipment(
        command_id=command_id,
        order_id=order_id,
        delivery_method_id=delivery_method_id,
        credentials_id=draft.get("allegro_credentials_id"),
        packages=packages,
        sender=sender,
        receiver=receiver,
        additional_properties=additional_properties,
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
            # Prefer new-format entries (with `date`); fall back to legacy `id`
            # (deprecated but still accepted by servers pre-2026-07-01).
            new_format = next((p for p in proposals if p.get("date")), None)
            legacy_format = next(
                (p for p in proposals if p.get("id") and not p.get("date")),
                None,
            )
            selected = new_format or legacy_format
            if selected:
                pu_cmd = str(_uuid.uuid4())
                if selected.get("date"):
                    pickup_time = {
                        "date": selected["date"],
                        "minTime": selected.get("minTime", "08:00"),
                        "maxTime": selected.get("maxTime", "18:00"),
                    }
                    client.create_ship_with_allegro_pickup(
                        command_id=pu_cmd,
                        shipment_ids=[shipment_id],
                        pickup_time=pickup_time,
                    )
                else:
                    # Legacy path (deprecated post-2026-07-01) — kept for
                    # sandbox/older-server compatibility.
                    client.create_ship_with_allegro_pickup(
                        command_id=pu_cmd,
                        shipment_ids=[shipment_id],
                        proposal_item_id=selected["id"],
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


def _assert_packages_fit_locker(
    breakdown: list[dict[str, Any]],
    *,
    carrier: str = "inpost",
) -> list[str]:
    """Sanity-check that every box in ``breakdown`` fits the carrier's largest locker slot.

    Returns a list of warning strings (empty if everything fits). We log each
    warning but do NOT hard-fail — an oversized parcel still ships via
    courier-to-door; we just can't hand it off at an automat. Used by
    ``_calc_packages`` for P2-3 sanity assertions.
    """
    from zdrovena.common.inpost import LOCKER_LARGE_SLOT, PARCEL_SPECS

    slot = LOCKER_LARGE_SLOT.get(carrier)
    if not slot:
        return []
    warnings: list[str] = []
    for box in breakdown:
        box_type = box.get("type", "")
        spec = PARCEL_SPECS.get(box_type)
        if not spec:
            continue
        # Sort sides so we compare shortest-to-shortest, etc. (rotation-invariant)
        pkg_sides = sorted([spec["length"], spec["width"], spec["height"]])
        slot_sides = sorted([slot["height"], slot["width"], slot["depth"]])
        if any(p > s for p, s in zip(pkg_sides, slot_sides, strict=True)):
            msg = (
                f"box '{box_type}' ({spec['length']}×{spec['width']}×{spec['height']} cm) "
                f"exceeds {carrier} locker large slot "
                f"({slot['height']}×{slot['width']}×{slot['depth']} cm)"
            )
            warnings.append(msg)
            logger.warning("_calc_packages: %s", msg)
        if spec["weight_kg"] > slot["max_weight_kg"]:
            msg = (
                f"box '{box_type}' weight {spec['weight_kg']} kg exceeds "
                f"{carrier} locker max {slot['max_weight_kg']} kg"
            )
            warnings.append(msg)
            logger.warning("_calc_packages: %s", msg)
    return warnings


def _calc_packages(
    product_items: list[dict[str, Any]],
) -> tuple[int, list[dict[str, Any]]]:
    """Return (packages_count, packages_breakdown) for a list of filtered line items.

    Plastik: greedy largest-box-first (3-pak → 2-pak → 1-pak → pół-pak).
    Szkło: greedy 2-pak consolidation (szkło-2pak → szkło for remainder).

    Post-condition (P2-3): every produced box is checked against the InPost
    ``LOCKER_LARGE_SLOT`` catalogue and any overflow is logged as a warning so
    operators can catch a mis-configured PARCEL_SPECS (e.g. a box larger than
    the paczkomat slot) early.
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

    # P2-3: sanity-check that every produced box fits the InPost large slot.
    # We log warnings only — an oversized parcel is still valid, it just can't
    # be handed off at a locker/automat.
    _assert_packages_fit_locker(breakdown, carrier="inpost")

    return max(total, 1), breakdown


def _create_draft_safely(
    order: dict[str, Any],
    shipping_store: ShippingStore,
    storage: Any,
    *,
    source: str = "shopify",
) -> None:
    """Wrapper around ``_create_draft`` that DLQs any exception (P1-9).

    ``BackgroundTasks`` provides no persistence — an exception here would be
    silently swallowed and the order would be lost. Instead we capture the
    payload + error to the DLQ so an operator can retry via
    ``POST /shipping/drafts/dlq/{entry_id}/retry``.
    """
    try:
        _create_draft(order, shipping_store, storage, source=source)
    except Exception as exc:
        logger.exception(
            "Draft creation failed for order %s (source=%s) — enqueueing to DLQ",
            order.get("id") or order.get("order_number"),
            source,
        )
        try:
            shipping_store.enqueue_dlq(
                payload=order,
                error=f"{type(exc).__name__}: {exc}",
                source=source,
            )
        except Exception:
            logger.exception(
                "DLQ enqueue itself failed for order %s",
                order.get("id") or order.get("order_number"),
            )


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
        apaczka_service_id: str | None = None
    else:
        courier = _pick_courier(order)
        inpost_service = _pick_inpost_service(title) if courier == "inpost" else None
        allegro_sending_method = None
        apaczka_service_id = _pick_apaczka_service(title) if courier == "apaczka" else None

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

    needs_review = (
        packages_count > 1
        or phone is None
        or (courier == "apaczka" and apaczka_service_id is None)
    )

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
        "apaczka_service_id": apaczka_service_id,
        "tracking_number": None,
        "courier_draft_id": None,
        "dispatch_order_id": None,  # fix #6: field exists from creation
        "status": "needs_review" if needs_review else "pending",
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
@router.post(
    # Legacy alias. Some Shopify webhook subscriptions in the shop admin still
    # point at /order-created (Shopify's own topic key is `orders/create`, but
    # the endpoint URL is operator-defined). Both paths execute the same
    # handler so renaming the primary path is never a breaking change.
    "/webhooks/shopify/order-created",
    status_code=status.HTTP_200_OK,
    summary="Shopify order webhook — legacy alias",
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

    # .strip() defends against proxies/tools that append trailing whitespace or
    # newlines to the header value (observed with cloudflared tunneled tests).
    sig_header = request.headers.get("X-Shopify-Hmac-Sha256", "").strip()
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
        logger.warning(
            "Shopify webhook with disallowed topic %r (id=%s) — rejected", topic, webhook_id
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Topic not allowed")
    if not _is_shopify_domain_allowed(shop_domain):
        logger.warning(
            "Shopify webhook from disallowed shop %r (id=%s) — rejected", shop_domain, webhook_id
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Shop domain not allowed")

    # 3. Parse the (now trusted) body.
    try:
        order = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON") from exc

    # 4. Deduplicate by X-Shopify-Webhook-Id atomically. `mark_seen_if_new` does
    #    a single check-and-set (Azure: create_entity+ResourceExistsError, local:
    #    load→check→save under flock) so two concurrent deliveries can never both
    #    proceed. Fail-closed (503) if the dedup store is unavailable so Shopify
    #    retries rather than us risking a duplicate draft.
    if webhook_id:
        try:
            inserted = dedup_store.mark_seen_if_new(webhook_id)
        except DedupStoreError:
            logger.exception("Shopify dedup store unavailable for webhook %s", webhook_id)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Dedup store unavailable",
            ) from None
        if not inserted:
            logger.info("Duplicate Shopify webhook %s — skipping", webhook_id)
            return {"status": "duplicate", "webhook_id": webhook_id}
    else:
        logger.warning("Shopify webhook missing X-Shopify-Webhook-Id — dedup skipped")

    # 5. Orders without shipping lines never become drafts.
    if not order.get("shipping_lines"):
        logger.warning("Order %s has no shipping_lines — skipping draft", order.get("id"))
        return {"status": "skipped"}

    # 6. Heavy work off the request path (Shopify enforces a 5s timeout).
    background_tasks.add_task(_create_draft_safely, order, shipping_store, storage)
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


@router.get(
    "/shipping/apaczka-services",
    summary="List the curated Apaczka courier services available for draft selection",
    responses={403: {"description": "Insufficient role"}},
)
def list_apaczka_services(
    principal: Annotated[Principal, Depends(require_viewer_or_above)],
) -> dict[str, Any]:
    from zdrovena.common.apaczka import APACZKA_SERVICE_CATALOG

    return {
        "services": [
            {"service_id": service_id, "label": label}
            for service_id, label in APACZKA_SERVICE_CATALOG.items()
        ]
    }


# ── Dead-letter queue (P1-9) ────────────────────────────────────────────────


@router.get(
    "/shipping/drafts/dlq",
    summary="List failed draft-creation attempts (DLQ)",
    responses={403: {"description": "Insufficient role"}},
)
def list_dlq(
    shipping_store: ShippingStoreDep,
    principal: Annotated[Principal, Depends(require_viewer_or_above)],
) -> dict[str, Any]:
    return {"entries": shipping_store.list_dlq()}


@router.post(
    "/shipping/drafts/dlq/{entry_id}/retry",
    summary="Retry a failed draft-creation attempt from DLQ",
    responses={
        403: {"description": "Insufficient role"},
        404: {"description": "DLQ entry not found"},
        502: {"description": "Retry failed — entry left in DLQ with updated error"},
    },
)
def retry_dlq_entry(
    entry_id: str,
    shipping_store: ShippingStoreDep,
    storage: StorageDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
) -> dict[str, Any]:
    entry = shipping_store.get_dlq_entry(entry_id)
    if not entry:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="DLQ entry not found")
    payload = entry.get("payload") or {}
    source = entry.get("source") or "shopify"
    try:
        _create_draft(payload, shipping_store, storage, source=source)
    except Exception as exc:
        logger.exception("DLQ retry failed for entry %s", entry_id)
        # bump retries + last_error; keep the entry in DLQ
        try:
            shipping_store.enqueue_dlq(
                payload=payload,
                error=f"{type(exc).__name__}: {exc}",
                source=source,
                entry_id=entry_id,
            )
        except Exception:
            logger.exception("DLQ update after retry failure failed for %s", entry_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Retry failed: {type(exc).__name__}: {exc}",
        ) from exc
    # success → remove from DLQ
    shipping_store.delete_dlq_entry(entry_id)
    return {"status": "retried", "entry_id": entry_id}


@router.delete(
    "/shipping/drafts/dlq/{entry_id}",
    summary="Discard a DLQ entry without retrying",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        403: {"description": "Insufficient role"},
        404: {"description": "DLQ entry not found"},
    },
)
def delete_dlq_entry(
    entry_id: str,
    shipping_store: ShippingStoreDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
) -> Response:
    entry = shipping_store.get_dlq_entry(entry_id)
    if not entry:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="DLQ entry not found")
    shipping_store.delete_dlq_entry(entry_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


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


# ── Confirm pending Allegro create-command ───────────────────────────────────


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
    draft = shipping_store.get_draft(draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")
    if draft.get("status") != "pending_confirmation":
        raise HTTPException(
            status_code=409,
            detail="Draft is not pending confirmation",
        )

    command_id = draft.get("allegro_command_id")
    if not command_id:
        raise HTTPException(
            status_code=409,
            detail="Draft has no allegro_command_id",
        )

    if _MOCK_COURIER:
        # Mock path: just flip to created so E2E tests can move on.
        patch = {
            "status": "created",
            "courier_draft_id": f"mock-allegro-{draft.get('shopify_order_number', 'x')}",
            "tracking_number": "AWA00000000",
            "error": None,
        }
        shipping_store.update_draft(draft_id, patch)
        return shipping_store.get_draft(draft_id) or patch

    client = _get_allegro_client()
    if client is None:
        raise HTTPException(status_code=502, detail="Allegro credentials missing")

    try:
        status_payload = client.get_ship_with_allegro_command_status(str(command_id))
    except (AllegroAuthError, CourierTransientError) as exc:
        logger.exception("Confirm poll failed for draft %s", draft_id)
        raise HTTPException(status_code=502, detail=f"Allegro API error: {exc}") from exc

    status = (status_payload or {}).get("status")
    if status == "IN_PROGRESS":
        # Still pending — return 202 so operator/worker can poll again later.
        return JSONResponse(
            status_code=202,
            content={
                "status": "pending_confirmation",
                "allegro_command_id": str(command_id),
                "draft_id": draft_id,
            },
        )
    if status == "ERROR":
        errors = status_payload.get("errors") or []
        detail = "; ".join(str(e.get("message") or e) for e in errors) or "Allegro command failed"
        patch = {
            "status": "error",
            "error": f"Allegro create-command {command_id} failed: {detail}",
        }
        shipping_store.update_draft(draft_id, patch)
        raise HTTPException(status_code=502, detail=patch["error"])
    if status != "SUCCESS":
        raise HTTPException(
            status_code=502,
            detail=f"Unexpected Allegro command status: {status!r}",
        )

    shipment_id = status_payload.get("shipmentId")
    if not shipment_id:
        raise HTTPException(
            status_code=502,
            detail="Allegro command SUCCESS but no shipmentId returned",
        )

    try:
        shipment = client.get_ship_with_allegro_shipment(str(shipment_id))
        _carrier_id, waybill = client.extract_shipment_waybill(shipment)
    except (AllegroBusinessError, AllegroAuthError, CourierTransientError) as exc:
        logger.exception("Fetching shipment %s failed", shipment_id)
        raise HTTPException(status_code=502, detail=f"Allegro API error: {exc}") from exc

    patch = {
        "status": "created",
        "courier_draft_id": str(shipment_id),
        "allegro_shipment_id": str(shipment_id),
        "tracking_number": waybill,
        "error": None,
    }
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

    # Claim before calling the courier (not after) so two concurrent requests
    # can't both pass the pickup_ordered check above and both dispatch.
    if not shipping_store.try_claim_pickup(draft_id):
        raise HTTPException(status_code=409, detail="Pickup already ordered")

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
            shipping_store.update_draft(draft_id, {"pickup_ordered": False})
            raise HTTPException(status_code=502, detail=f"InPost dispatch error: {exc}") from exc

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

    shipping_store.update_draft(draft_id, {"status": "cancelled", "allegro_shipment_id": None})
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

    shipping_store.update_draft(draft_id, {"pickup_ordered": False, "allegro_dispatch_id": None})
    return {"status": "dispatch_cancelled", "draft_id": draft_id, "dispatch_id": str(dispatch_id)}


# ── Cancel (raw courier id: InPost / Apaczka) ─────────────────────────────────


# Manual fulfillment marking (generic; Allegro side-effect kept for allegro drafts)


@router.post(
    "/shipping/drafts/{draft_id}/mark-fulfilled",
    summary="Manually mark the draft as fulfilled (operator action)",
    responses={
        403: {"description": "Insufficient role"},
        404: {"description": "Draft not found"},
        409: {"description": "Allegro draft has no external Allegro order id"},
        502: {"description": "Allegro API error (only for Allegro drafts)"},
    },
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
    ``AllegroClient.mark_order_processed(external_order_id)`` to move the order
    to ``PROCESSING`` on Allegro's side, and mirror the timestamps into the
    legacy ``allegro_fulfillment_status`` / ``allegro_marked_processed_*`` fields.

    Re-running this endpoint is safe: if the draft is already fulfilled we
    return 200 without hitting Allegro again.
    """
    draft = shipping_store.get_draft(draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")

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
        }

    allegro_side_effect = False
    if is_allegro and not _MOCK_COURIER:
        client = _get_allegro_client()
        if client is None:
            raise HTTPException(status_code=502, detail="Allegro credentials missing")
        try:
            client.mark_order_processed(str(external_order_id))
            allegro_side_effect = True
        except (AllegroBusinessError, AllegroAuthError, CourierTransientError) as exc:
            logger.exception("Allegro mark_order_processed failed for draft %s", draft_id)
            raise HTTPException(status_code=502, detail=f"Allegro API error: {exc}") from exc
    elif is_allegro and _MOCK_COURIER:
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
        patch["allegro_fulfillment_status"] = "PROCESSING"
        patch["allegro_marked_processed_at"] = marked_at
        patch["allegro_marked_processed_by"] = marked_by

    shipping_store.update_draft(draft_id, patch)
    return {
        "status": "marked_fulfilled",
        "draft_id": draft_id,
        "source": draft.get("source"),
        "external_order_id": external_order_id,
        "fulfilled_at": marked_at,
        "fulfilled_by": marked_by,
        "allegro_side_effect": allegro_side_effect,
    }


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
    # No draft available here (only order_id) and cancel_shipment() never
    # reads service_id — pass an empty placeholder rather than looking one up.
    client = ApaczkaClient(app_id, app_secret, "", storage)
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
    apaczka_service_id: str | None = Body(None),
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
    if apaczka_service_id is not None:
        from zdrovena.common.apaczka import APACZKA_SERVICE_CATALOG

        if apaczka_service_id not in APACZKA_SERVICE_CATALOG:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown apaczka_service_id: {apaczka_service_id}",
            )
        patch["apaczka_service_id"] = apaczka_service_id
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
    courier: str = Query(
        None, description="inpost, apaczka, or allegro_delivery (defaults to draft's courier)"
    ),
) -> StreamingResponse:
    draft = shipping_store.get_draft(draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")

    # Prefer the stored draft courier over the query param (prevents mismatch)
    courier = draft.get("courier") or courier
    _SUPPORTED_COURIERS = ("inpost", "apaczka", "allegro_delivery")
    if courier not in _SUPPORTED_COURIERS:
        raise HTTPException(
            status_code=400,
            detail=f"courier must be one of: {', '.join(_SUPPORTED_COURIERS)}",
        )

    # For Ship-with-Allegro the label id is the shipment_id, stored under courier_draft_id
    # (set by _run_allegro_delivery) and mirrored to allegro_shipment_id.
    if courier == "allegro_delivery":
        label_id = draft.get("allegro_shipment_id") or draft.get("courier_draft_id")
    else:
        label_id = draft.get("courier_draft_id")
    if not label_id:
        raise HTTPException(status_code=404, detail="No courier draft ID — draft may have failed")

    try:
        if courier == "inpost":
            from zdrovena.common.inpost import InPostClient

            token = get_secret("inpost_api_token")
            org_id = get_secret("inpost_organization_id")
            pdf_bytes = InPostClient(token, org_id).get_label(label_id)
        elif courier == "apaczka":
            from zdrovena.common.apaczka import ApaczkaClient

            app_id = get_secret("apaczka_app_id")
            app_secret = get_secret("apaczka_app_secret")
            # get_label() never reads service_id (verified in apaczka.py), but
            # pass the real per-draft value anyway for consistency/future-proofing.
            service_id = draft.get("apaczka_service_id") or ""
            pdf_bytes = ApaczkaClient(app_id, app_secret, service_id, storage).get_label(label_id)
        elif courier == "allegro_delivery":
            client = _get_allegro_client()
            if client is None:
                raise HTTPException(status_code=502, detail="Allegro credentials missing")
            try:
                pdf_bytes = client.get_ship_with_allegro_label(str(label_id))
            except (AllegroBusinessError, AllegroAuthError, CourierTransientError) as exc:
                logger.exception("Allegro label fetch failed for draft %s", draft_id)
                raise HTTPException(status_code=502, detail=f"Allegro API error: {exc}") from exc
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
