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
from math import ceil
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
from zdrovena.api.observability import correlation_scope, get_correlation_id
from zdrovena.audit.bottles import SKIP_RE, bottles_per_unit, is_glass
from zdrovena.common.appenv import is_production_env
from zdrovena.common.events import log_event
from zdrovena.common.secrets import get_secret
from zdrovena.common.shipping_exceptions import (
    AllegroAuthError,
    AllegroBusinessError,
    AllegroCommandPending,
    ApaczkaBusinessError,
    CourierAuthError,
    CourierBusinessError,
    CourierTransientError,
    InPostBusinessError,
    LabelNotReadyError,
    ZdrovenaShippingError,
)
from zdrovena.common.shipping_format import (
    extract_locker_id_from_title,
    normalize_pl_phone,
    parse_pl_address,
)
from zdrovena.common.shipping_state import EXECUTING
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


def _get_fakturownia_client() -> Any | None:
    """Build a FakturowniaClient from Key Vault secrets. Returns None if missing."""
    from zdrovena.common.config import KEYCHAIN_SERVICE_FAKTUROWNIA
    from zdrovena.common.exceptions import MissingSecretError

    try:
        token = get_secret(KEYCHAIN_SERVICE_FAKTUROWNIA)
    except MissingSecretError:
        return None
    from zdrovena.common.client import FakturowniaClient

    return FakturowniaClient(api_token=token)


def _sync_shopify_orders_from_api(
    shop_domain: str,
    api_token: str,
    shipping_store: ShippingStore,
    storage: Any,
) -> dict[str, int]:
    """Fetch Shopify orders via REST API and create or refresh shipping drafts.

    Uses external_order_id (Shopify order id) for idempotency. Existing drafts
    are refreshed instead of skipped so the visible list reflects status changes
    made outside this app.
    """
    import requests

    stats: dict[str, int] = {
        "fetched": 0,
        "created": 0,
        "updated": 0,
        "unchanged": 0,
        "errors": 0,
    }
    # limit=50 is intentional for v1 — covers typical daily volume.
    # If >50 recently open orders pile up, a second sync call will catch the rest.
    # No cursor pagination implemented.
    resp = requests.get(
        f"https://{shop_domain}/admin/api/2024-01/orders.json",
        params={
            "status": "any",
            "fulfillment_status": "any",
            "order": "updated_at desc",
            "limit": 50,
            "fields": (
                "id,order_number,name,email,phone,created_at,updated_at,"
                "cancelled_at,closed_at,financial_status,fulfillment_status,"
                "fulfillments,shipping_address,shipping_lines,line_items,"
                "note_attributes,customer"
            ),
        },
        headers={"X-Shopify-Access-Token": api_token},
        timeout=15,
    )
    resp.raise_for_status()
    orders = resp.json().get("orders", [])
    stats["fetched"] = len(orders)
    if not orders:
        return stats

    # High limit: list_drafts fetches all Table Storage rows anyway; the cap
    # only affects the returned slice. 10_000 covers any realistic store size
    # and prevents silent duplicate-draft creation on stores with >200 total orders.
    existing_drafts = shipping_store.list_drafts(limit=10_000)
    existing_by_order_id = {
        str(d.get("external_order_id", "")): d
        for d in existing_drafts
        if d.get("source") == "shopify" and d.get("external_order_id")
    }

    for order in orders:
        order_id = str(order.get("id", ""))
        try:
            existing = existing_by_order_id.get(order_id)
            changed = _sync_draft_from_order(
                order,
                shipping_store,
                storage,
                source="shopify",
                existing=existing,
            )
            if existing is None:
                stats["created"] += 1
            elif changed:
                stats["updated"] += 1
            else:
                stats["unchanged"] += 1
        except Exception:
            logger.exception(
                "Shopify sync: draft refresh failed for order %s", order.get("order_number")
            )
            stats["errors"] += 1

    return stats


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

    shopify_token = get_secret("shopify_admin_token", required=False)
    if not shopify_token:
        return {"skipped": "shopify_not_configured"}

    allowed_domains = _allowed_shopify_domains()
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


_OCTOLIZE_PROVIDER_CODES = {
    "8828": "poczta",
    "8829": "inpost",
    "8830": "dpd",
}
_PICKUP_PROVIDER_ALIASES = {
    "dpd": "dpd",
    "inpost": "inpost",
    "poczta": "poczta",
    "poczta polska": "poczta",
    "pocztex": "poczta",
}
_APACZKA_PICKUP_SERVICES = {
    "dpd": "23",  # DPD Pickup Drzwi-Punkt
    "poczta": "64",  # Pocztex Kurier Drzwi-Punkt
}
_APACZKA_SERVICES_REQUIRING_PICKUP_POINT = frozenset(_APACZKA_PICKUP_SERVICES.values())


def _normalize_pickup_provider(value: Any) -> str | None:
    normalized = " ".join(str(value or "").strip().lower().split())
    return _PICKUP_PROVIDER_ALIASES.get(normalized)


def _extract_shopify_pickup_point(order: dict[str, Any]) -> dict[str, str] | None:
    """Extract trusted Octolize pickup-point metadata from a Shopify order.

    The human-readable shipping title contains a shop name and distance, so it
    changes for every order. Octolize also supplies stable structured fields:
    ``shipping_lines[].code`` identifies the provider and ``PickupPointId`` is
    the courier's external point identifier. Prefer those fields and keep title
    parsing only as a point-id fallback for older payloads.
    """
    shipping_lines = order.get("shipping_lines") or []
    line = shipping_lines[0] if shipping_lines else {}
    code = str(line.get("code") or "").strip()
    source = str(line.get("source") or "").strip().lower()
    note_attrs = {
        str(attr.get("name") or ""): str(attr.get("value") or "").strip()
        for attr in (order.get("note_attributes") or [])
        if attr.get("name")
    }

    code_parts = code.split(":")
    provider_from_code = (
        _OCTOLIZE_PROVIDER_CODES.get(code_parts[1])
        if len(code_parts) >= 2 and code_parts[0] == "pickup-points"
        else None
    )
    provider_from_note = _normalize_pickup_provider(note_attrs.get("PickupPointCourier"))
    is_octolize = (
        bool(code_parts) and code_parts[0] == "pickup-points"
    ) or source == "octolize pick-up points pro"
    if not is_octolize:
        return None

    provider = provider_from_code or provider_from_note or ""
    if provider_from_code and provider_from_note and provider_from_code != provider_from_note:
        logger.warning(
            "Shopify pickup provider mismatch: code=%s note=%s order=%s; "
            "using the structured shipping-line code",
            provider_from_code,
            provider_from_note,
            order.get("order_number") or order.get("id"),
        )

    title = str(line.get("title") or "")
    point_id = note_attrs.get("PickupPointId") or extract_locker_id_from_title(title)
    return {
        "provider": provider,
        "id": point_id,
        "name": note_attrs.get("PickupPointName", ""),
        "address": note_attrs.get("PickupPointAddress", ""),
        "post_code": note_attrs.get("PickupPointPostCode", ""),
        "city": note_attrs.get("PickupPointCity", ""),
    }


def _reset_courier_maps_cache() -> None:
    """Clear cached ENV mapping (test-only helper)."""
    _courier_title_map.cache_clear()
    _inpost_service_title_map.cache_clear()
    _apaczka_service_title_map.cache_clear()


_MATCH_AUTO = "auto_matched"
_MATCH_MANUAL = "manual"
_MATCH_REQUIRES_SELECTION = "requires_selection"
_MATCH_UNRECOGNIZED = "unrecognized"
_MATCH_FIELDS = (
    "shipping_service_match_status",
    "shipping_service_match_source",
    "shipping_service_match_detail",
)


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
    if "inpost" in title or "paczkomat" in title or "drzwi" in title:
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


def _shipping_service_match_fields(
    *,
    courier: str,
    title: str,
    inpost_service: str | None,
    apaczka_service_id: str | None,
    allegro_method_id: str | None,
    pickup_point: dict[str, str] | None = None,
) -> dict[str, str | None]:
    source_title = (title or "").strip() or None
    if courier == "allegro_delivery":
        return {
            "shipping_service_match_status": _MATCH_AUTO
            if allegro_method_id
            else _MATCH_UNRECOGNIZED,
            "shipping_service_match_source": source_title or allegro_method_id,
            "shipping_service_match_detail": "Allegro delivery method id matched",
        }
    if courier == "inpost":
        return {
            "shipping_service_match_status": _MATCH_AUTO if inpost_service else _MATCH_UNRECOGNIZED,
            "shipping_service_match_source": source_title,
            "shipping_service_match_detail": (
                "InPost service matched from shipping method"
                if inpost_service
                else "No InPost service mapping matched"
            ),
        }
    if courier == "apaczka" and apaczka_service_id:
        structured_provider = (pickup_point or {}).get("provider")
        return {
            "shipping_service_match_status": _MATCH_AUTO,
            "shipping_service_match_source": source_title,
            "shipping_service_match_detail": (
                f"Apaczka service matched from Shopify pickup provider {structured_provider}"
                if structured_provider
                else "Apaczka service matched from APACZKA_SERVICE_TITLE_MAP"
            ),
        }
    if courier == "apaczka" and pickup_point and pickup_point.get("provider"):
        detail = (
            "Shopify pickup point is missing PickupPointId"
            if not pickup_point.get("id")
            else f"No Apaczka service mapping for pickup provider {pickup_point['provider']}"
        )
        return {
            "shipping_service_match_status": _MATCH_REQUIRES_SELECTION,
            "shipping_service_match_source": source_title,
            "shipping_service_match_detail": detail,
        }
    return {
        "shipping_service_match_status": _MATCH_REQUIRES_SELECTION
        if source_title
        else _MATCH_UNRECOGNIZED,
        "shipping_service_match_source": source_title,
        "shipping_service_match_detail": (
            "No Apaczka service mapping matched" if source_title else "No source shipping method"
        ),
    }


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
            receiver_building_number="/".join(
                filter(None, [addr.get("building_number", "1"), addr.get("flat_number", "")])
            ),
            receiver_city=addr.get("city", ""),
            receiver_post_code=addr.get("post_code", ""),
            sender=sender,
            reference=reference,
            weight_kg=weight_kg,
            dimensions=dims,
        )

    return {
        "courier_draft_id": str(result.get("id", "")),
        "dispatch_order_id": None,
        "tracking_number": result.get("tracking_number"),
        "status": "created",
        "pickup_ordered": False,
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
    receiver = draft.get("receiver") or {}
    pickup_point = draft.get("pickup_point") or {}
    receiver_point_id = str(pickup_point.get("id") or receiver.get("locker_id") or "").strip()
    if service_id in _APACZKA_SERVICES_REQUIRING_PICKUP_POINT and not receiver_point_id:
        raise ApaczkaBusinessError(
            f"Draft {draft.get('id')} uses Apaczka point service {service_id} "
            "but has no pickup point id",
            order_id=str(draft.get("id", "")),
            courier="apaczka",
            action="create_shipment",
        )
    client = ApaczkaClient(app_id, app_secret, service_id, storage)
    addr = draft.get("shipping_address") or {}
    customer_name = f"{receiver.get('first_name', '')} {receiver.get('last_name', '')}".strip()
    result = client.create_shipment(
        receiver_name=customer_name,
        receiver_firstname=receiver.get("first_name", ""),
        receiver_lastname=receiver.get("last_name", ""),
        receiver_email=receiver.get("email", ""),
        receiver_phone=receiver.get("phone", ""),
        receiver_address=" ".join(
            filter(
                None,
                [
                    addr.get("street", ""),
                    addr.get("building_number", ""),
                    addr.get("flat_number", ""),
                ],
            )
        ),
        receiver_city=addr.get("city", ""),
        receiver_zip=addr.get("post_code", ""),
        receiver_point_id=receiver_point_id or None,
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
    plastic_half_packs = 0
    glass_half_packs = 0
    for item in product_items:
        qty = item.get("quantity", 1)
        name = item.get("name", "")
        bottle_count = bottles_per_unit(name)
        half_packs = ceil(float(qty) * bottle_count / 6) if bottle_count else int(qty) * 2
        if is_glass(item.get("name", "")):
            glass_half_packs += half_packs
        else:
            plastic_half_packs += half_packs

    breakdown: list[dict[str, Any]] = []

    # Plastik — greedy
    remaining = plastic_half_packs // 2
    for box_size, label in ((3, "3-pak"), (2, "2-pak"), (1, "1-pak")):
        if remaining >= box_size:
            count = remaining // box_size
            breakdown.append({"type": label, "qty": count})
            remaining -= count * box_size
    if plastic_half_packs % 2:
        breakdown.append({"type": "pół-pak", "qty": 1})

    # Szkło — greedy: 2-pak first, then single boxes
    remaining_glass = (glass_half_packs + 1) // 2
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
    correlation_id: str = "-",
) -> None:
    """Wrapper around ``_create_draft`` that DLQs any exception (P1-9).

    ``BackgroundTasks`` provides no persistence — an exception here would be
    silently swallowed and the order would be lost. Instead we capture the
    payload + error to the DLQ so an operator can retry via
    ``POST /shipping/drafts/dlq/{entry_id}/retry``.

    ``correlation_id`` jest ustawiany na starcie, aby logi tworzenia draftu w tle
    dzieliły identyfikator z logiem webhooka, który je zakolejkował. Używamy
    ``correlation_scope`` (token/reset w ``finally``), aby ID nie wyciekło do
    kolejnego zadania tła na tym samym workerze (R4-B).
    """
    with correlation_scope(correlation_id):
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


_SYNC_PRESERVED_FIELDS = {
    "id",
    "created_at",
    "courier_draft_id",
    "dispatch_order_id",
    "allegro_shipment_id",
    "allegro_dispatch_id",
    "pickup_ordered",
    "fakturownia_invoice_id",
    "fakturownia_invoice_number",
    "fakturownia_invoice_error",
    "fakturownia_invoice_attempts",
    "fakturownia_invoice_attempted_at",
    "allegro_fulfillment_status",
}

_SYNC_TERMINAL_STATUSES = {"created", "cancelled"}
_SYNC_BUSY_STATUSES = {"executing", "pending_confirmation"}


def _source_fulfillment_status(order: dict[str, Any], *, source: str) -> str | None:
    raw = str(order.get("fulfillment_status") or "").strip().lower()
    if source == "allegro":
        if raw in {"sent", "picked_up"}:
            return "fulfilled"
        if raw in {"processing", "ready_for_shipment"}:
            return "processing"
        if raw:
            return raw
        return None
    if raw == "fulfilled":
        return "fulfilled"
    if raw == "partial":
        return "partial"
    if raw:
        return raw
    fulfillments = order.get("fulfillments") or []
    if fulfillments:
        return "fulfilled"
    return None


def _source_cancelled(order: dict[str, Any]) -> bool:
    return bool(order.get("cancelled_at") or order.get("cancelled") is True)


def _source_fulfillment_details(order: dict[str, Any]) -> dict[str, Any]:
    fulfillments = order.get("fulfillments") or []
    if not isinstance(fulfillments, list):
        return {}
    for fulfillment in fulfillments:
        if not isinstance(fulfillment, dict):
            continue
        tracking_number = fulfillment.get("tracking_number")
        tracking_numbers = fulfillment.get("tracking_numbers")
        if not tracking_number and isinstance(tracking_numbers, list) and tracking_numbers:
            tracking_number = tracking_numbers[0]
        if tracking_number:
            return {
                "tracking_number": tracking_number,
                "tracking_company": fulfillment.get("tracking_company"),
                "fulfilled_at": fulfillment.get("updated_at") or fulfillment.get("created_at"),
                "shopify_fulfillment_id": str(fulfillment.get("id", "")) or None,
            }
    return {}


def _status_from_source(order: dict[str, Any], fallback: str, *, source: str) -> str:
    source_fulfillment = _source_fulfillment_status(order, source=source)
    if _source_cancelled(order):
        return "cancelled"
    if source_fulfillment == "cancelled":
        return "cancelled"
    if source_fulfillment == "fulfilled":
        return "created"
    return fallback


def _build_draft_record(
    order: dict[str, Any],
    *,
    source: str = "shopify",
    draft_id: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
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
    pickup_point = _extract_shopify_pickup_point(order)

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
        allegro_sending_method = None
        pickup_provider = (pickup_point or {}).get("provider")
        pickup_point_id = (pickup_point or {}).get("id")
        if pickup_provider == "inpost":
            courier = "inpost"
            inpost_service = "paczkomat"
            apaczka_service_id = None
        elif pickup_provider in _APACZKA_PICKUP_SERVICES:
            courier = "apaczka"
            inpost_service = None
            apaczka_service_id = (
                _APACZKA_PICKUP_SERVICES[pickup_provider] if pickup_point_id else None
            )
        else:
            courier = _pick_courier(order)
            inpost_service = _pick_inpost_service(title) if courier == "inpost" else None
            apaczka_service_id = _pick_apaczka_service(title) if courier == "apaczka" else None

    line_items = order.get("line_items") or []
    product_items = [item for item in line_items if not SKIP_RE.search(item.get("name", ""))]
    total_qty = max(sum(item.get("quantity", 1) for item in product_items), 1)
    packages_count, packages_breakdown = _calc_packages(product_items)
    if inpost_service == "paczkomat":
        locker_id = (
            (pickup_point or {}).get("id")
            or extract_locker_id_from_title(title)
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

    needs_review = phone is None or (courier == "apaczka" and apaczka_service_id is None)
    if (
        courier == "apaczka"
        and apaczka_service_id in _APACZKA_SERVICES_REQUIRING_PICKUP_POINT
        and not (pickup_point or {}).get("id")
    ):
        needs_review = True

    source_fulfillment = _source_fulfillment_status(order, source=source)
    now = datetime.now(timezone.utc).isoformat()
    base_status = "needs_review" if needs_review else "pending"
    fulfillment_details = _source_fulfillment_details(order) if source == "shopify" else {}
    record: dict[str, Any] = {
        "id": draft_id or str(uuid.uuid4()),
        "created_at": created_at or now,
        "updated_at": now,
        "order_date": order.get("created_at"),
        "source": source,
        "external_order_id": order_id,
        "shopify_order_id": order_id if source == "shopify" else None,
        "shopify_order_number": str(order_number),
        "customer_name": customer_name,
        "courier": courier,
        "service": service,
        "apaczka_service_id": apaczka_service_id,
        "pickup_point": pickup_point,
        **_shipping_service_match_fields(
            courier=courier,
            title=title,
            inpost_service=inpost_service,
            apaczka_service_id=apaczka_service_id,
            allegro_method_id=allegro_method_id,
            pickup_point=pickup_point,
        ),
        "tracking_number": fulfillment_details.get("tracking_number"),
        "tracking_company": fulfillment_details.get("tracking_company"),
        "courier_draft_id": None,
        "dispatch_order_id": None,  # fix #6: field exists from creation
        "status": _status_from_source(order, base_status, source=source),
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
            "flat_number": shipping_addr.get("address2", ""),
            "city": shipping_addr.get("city", ""),
            "post_code": shipping_addr.get("zip", ""),
        },
        "parcel": {"template": "large", "weight_kg": None},  # fix #4: large is safe default
        "error": None,
        "source_order_status": order.get("financial_status") or order.get("status"),
        "source_fulfillment_status": order.get("fulfillment_status"),
        "fulfillment_status": source_fulfillment,
        "fulfilled_at": fulfillment_details.get("fulfilled_at"),
        "shopify_fulfillment_id": fulfillment_details.get("shopify_fulfillment_id"),
        "cancelled_at": order.get("cancelled_at"),
        "source_updated_at": order.get("updated_at"),
        "fakturownia_invoice_id": None,
        "fakturownia_invoice_number": None,
        "fakturownia_invoice_error": None,
        "fakturownia_invoice_attempts": 0,
        "fakturownia_invoice_attempted_at": None,
    }

    # Wysyłam z Allegro — dodatkowe pola potrzebne dla /shipment-management/*
    if courier == "allegro_delivery":
        record["allegro_delivery_method_id"] = allegro_method_id
        record["allegro_credentials_id"] = None  # Allegro Standard; nadpisze się dla własnej umowy
        record["allegro_sending_method"] = allegro_sending_method

    return record


def _merge_synced_draft(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = {**existing, **incoming}
    for field in _SYNC_PRESERVED_FIELDS:
        if field in existing:
            merged[field] = existing[field]
    if existing.get("fulfilled_at") and not incoming.get("fulfilled_at"):
        merged["fulfilled_at"] = existing["fulfilled_at"]

    existing_status = existing.get("status")
    incoming_status = incoming.get("status")

    if existing_status in _SYNC_TERMINAL_STATUSES or existing_status in _SYNC_BUSY_STATUSES:
        merged["status"] = existing_status
    elif incoming_status == "created":
        merged["status"] = "created"
        if not merged.get("fulfilled_at"):
            merged["fulfilled_at"] = incoming.get("source_updated_at") or incoming.get("updated_at")
    elif incoming_status == "cancelled":
        merged["status"] = "cancelled"
    elif existing_status == "pending" and incoming_status == "needs_review":
        merged["status"] = "pending"
    else:
        merged["status"] = incoming_status or existing_status

    if (
        existing.get("fulfillment_status") == "fulfilled"
        or incoming.get("fulfillment_status") == "fulfilled"
    ):
        merged["fulfillment_status"] = "fulfilled"

    if existing.get("apaczka_service_id") and incoming.get("courier") == existing.get("courier"):
        merged["apaczka_service_id"] = existing["apaczka_service_id"]
        if existing.get("shipping_service_match_status") == _MATCH_MANUAL:
            for field in _MATCH_FIELDS:
                if field in existing:
                    merged[field] = existing[field]
    if existing.get("service") and existing_status in _SYNC_BUSY_STATUSES | _SYNC_TERMINAL_STATUSES:
        merged["service"] = existing["service"]
        merged["courier"] = existing.get("courier", merged.get("courier"))
    if existing.get("tracking_number"):
        merged["tracking_number"] = existing["tracking_number"]
        merged["tracking_company"] = existing.get("tracking_company")

    return merged


def _meaningful_draft_diff(before: dict[str, Any], after: dict[str, Any]) -> bool:
    ignored = {"updated_at"}
    keys = (set(before) | set(after)) - ignored
    return any(before.get(key) != after.get(key) for key in keys)


def _persist_draft_from_order(
    order: dict[str, Any],
    shipping_store: ShippingStore,
    storage: Any,
    *,
    source: str = "shopify",
    existing: dict[str, Any] | None = None,
) -> tuple[bool, dict[str, Any]]:
    record = _build_draft_record(
        order,
        source=source,
        draft_id=existing.get("id") if existing else None,
        created_at=existing.get("created_at") if existing else None,
    )
    if existing is not None:
        record = _merge_synced_draft(existing, record)
    changed = existing is None or _meaningful_draft_diff(existing, record)
    if changed:
        shipping_store.upsert_draft(record)
    if existing is None:
        log_event(
            "draft.created",
            order_number=record["shopify_order_number"],
            draft_id=record["id"],
            source=source,
            courier=record["courier"],
            status=record["status"],
            packages_count=record["packages_count"],
        )
        _maybe_send_new_order_sms(record)
    elif changed:
        log_event(
            "draft.updated_from_sync",
            order_number=record["shopify_order_number"],
            draft_id=record["id"],
            source=source,
            status=record["status"],
            fulfillment_status=record.get("fulfillment_status"),
        )
    return changed, record


def _sync_draft_from_order(
    order: dict[str, Any],
    shipping_store: ShippingStore,
    storage: Any,
    *,
    source: str = "shopify",
    existing: dict[str, Any] | None = None,
) -> bool:
    changed, _ = _persist_draft_from_order(
        order,
        shipping_store,
        storage,
        source=source,
        existing=existing,
    )
    return changed


def _create_draft(
    order: dict[str, Any],
    shipping_store: ShippingStore,
    storage: Any,
    *,
    source: str = "shopify",
) -> dict[str, Any]:
    _, record = _persist_draft_from_order(order, shipping_store, storage, source=source)
    return record


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
    #    Correlation ID przekazujemy jawnie — kontekst żądania jest już zresetowany,
    #    gdy Starlette wykonuje zadanie tła, więc log draftu inaczej straciłby powiązanie.
    background_tasks.add_task(
        _create_draft_safely,
        order,
        shipping_store,
        storage,
        correlation_id=get_correlation_id(),
    )
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
        # DLQ retry to endpoint diagnostyczny operatora — surowy błąd upstream
        # jest tu celowo zwracany, żeby operator mógł zdecydować o dalszej akcji.
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


# ── Fake-provider E2E support ────────────────────────────────────────────────


@router.post(
    "/__test__/shipping/reset",
    include_in_schema=False,
    responses={404: {"description": "Disabled outside fake non-production mode"}},
)
def reset_e2e_shipping_state(
    shipping_store: ShippingStoreDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
) -> dict[str, int]:
    _require_test_support()
    removed_drafts = 0
    for draft in shipping_store.list_drafts(limit=200):
        if _is_e2e_record(draft):
            shipping_store.delete_draft(str(draft["id"]))
            removed_drafts += 1

    removed_dlq = 0
    for entry in shipping_store.list_dlq(limit=200):
        if _is_e2e_record(entry):
            shipping_store.delete_dlq_entry(str(entry["id"]))
            removed_dlq += 1

    return {"removed_drafts": removed_drafts, "removed_dlq": removed_dlq}


@router.post(
    "/__test__/shipping/drafts",
    include_in_schema=False,
    responses={404: {"description": "Disabled outside fake non-production mode"}},
)
def seed_e2e_shipping_draft(
    draft: Annotated[dict[str, Any], Body()],
    shipping_store: ShippingStoreDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
) -> dict[str, Any]:
    _require_test_support()
    if not _is_e2e_record(draft):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="E2E draft id must start with e2e- or order number with 990",
        )
    if not draft.get("id"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Draft id required")
    shipping_store.upsert_draft(draft)
    return shipping_store.get_draft(str(draft["id"])) or draft


@router.post(
    "/__test__/shipping/dlq",
    include_in_schema=False,
    responses={404: {"description": "Disabled outside fake non-production mode"}},
)
def seed_e2e_dlq_entry(
    body: Annotated[dict[str, Any], Body()],
    shipping_store: ShippingStoreDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
) -> dict[str, Any]:
    _require_test_support()
    payload = body.get("payload") or {}
    entry_id = str(body.get("id") or "")
    probe = {"id": entry_id, "payload": payload}
    if not _is_e2e_record(probe):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="E2E DLQ id must start with e2e- or payload order number with 990",
        )
    return shipping_store.enqueue_dlq(
        payload=payload,
        error=str(body.get("error") or "E2E seeded failure"),
        source=str(body.get("source") or "shopify"),
        entry_id=entry_id or None,
    )


# ── Execute draft ─────────────────────────────────────────────────────────────


def _release_execution_claim(shipping_store: ShippingStore, draft_id: str, error: str) -> None:
    """Conditionally return a claimed draft to ``error`` (R5-A/#136).

    Only acts when the draft is still ``executing`` — i.e. the claim was taken
    but no legitimate final state was reached. If the happy path already wrote a
    later state (``created``), or a concurrent actor changed it, this is a no-op,
    so cleanup never clobbers a good state. ``error`` is an executable state, so a
    subsequent retry can re-claim the draft.

    Best-effort: a failure to write the cleanup is logged, not raised, so it
    cannot mask the original exception being handled.
    """
    try:
        current = shipping_store.get_draft(draft_id)
        if current and current.get("status") == EXECUTING:
            shipping_store.update_draft(draft_id, {"status": "error", "error": error})
    except Exception:
        logger.exception("Failed to release execution claim for draft %s (left as-is)", draft_id)


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
    # Atomic execution claim (R5-A): move the draft to `executing` under
    # optimistic concurrency. If the claim fails the draft is already
    # executing/created/cancelled or a concurrent request won the race — either
    # way we must not call the courier again (that would duplicate the shipment).
    if not shipping_store.try_claim_execution(draft_id):
        raise HTTPException(
            status_code=409,
            detail="Draft already executed or in progress — nie realizuj ponownie.",
        )

    # From here on the draft is claimed (status=executing). EVERY path to the end
    # of the endpoint must be guarded so an exception before a legitimate final
    # state cannot leave the draft stuck in `executing` (R5-A/#136). Cleanup is
    # conditional — see _release_execution_claim — so it never clobbers a state
    # the happy path already wrote (e.g. `created`).
    try:
        pickup_schedule = {
            "pickup_date": pickup_date,
            "pickup_from": pickup_from,
            "pickup_to": pickup_to,
        }
        sender = _get_sender()
        courier = draft.get("courier", "apaczka")
        if courier == "allegro_delivery":
            patch = _run_allegro_delivery(draft, storage, **pickup_schedule)
        elif courier == "inpost":
            patch = _run_inpost(draft, sender, **pickup_schedule)
        else:
            patch = _run_apaczka(draft, sender, storage, **pickup_schedule)

        # Success write moves executing → created. If THIS fails, the draft is
        # still `executing`, and the except-block cleanup returns it to `error`.
        shipping_store.update_draft(draft_id, patch)
        updated = shipping_store.get_draft(draft_id)
        log_event(
            "shipment.created",
            draft_id=draft_id,
            order_number=draft.get("shopify_order_number"),
            courier=draft.get("courier"),
            tracking_number=patch.get("tracking_number"),
            status=patch.get("status"),
        )
        if updated:
            # Never re-raises (see its docstring) — safe inside the guarded block.
            _maybe_push_tracking_to_allegro(updated)
        return updated or patch
    except ZdrovenaShippingError as exc:
        logger.exception("execute_draft failed for %s", draft_id)
        _release_execution_claim(shipping_store, draft_id, str(exc))
        # Wyjątek domenowy przesyłki → koperta błędu (zdrovena.api.errors)
        # mapuje go na właściwy status i polski komunikat dla operatora.
        raise
    except Exception as exc:
        logger.exception("execute_draft failed for %s", draft_id)
        _release_execution_claim(shipping_store, draft_id, str(exc))
        # Ogólny błąd komunikacji z przewoźnikiem → 502 z polskim komunikatem,
        # bez wyciekania surowego (angielskiego) str(exc) do operatora.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Błąd komunikacji z przewoźnikiem — spróbuj ponownie za chwilę.",
        ) from exc


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
    if is_allegro and not _MOCK_COURIER:
        client = _get_allegro_client()
        if client is None:
            raise HTTPException(status_code=502, detail="Allegro credentials missing")
        try:
            client.mark_order_processed(str(external_order_id), status="SENT")
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
        patch["shipping_service_match_status"] = _MATCH_MANUAL
        patch["shipping_service_match_source"] = "operator"
        patch["shipping_service_match_detail"] = "Manual service override"
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
        patch["shipping_service_match_status"] = _MATCH_MANUAL
        patch["shipping_service_match_source"] = "operator"
        patch["shipping_service_match_detail"] = "Manual Apaczka service override"
    if reviewed is True and draft.get("status") == "needs_review":
        patch["status"] = "pending"
        patch["error"] = None

    if patch:
        shipping_store.update_draft(draft_id, patch)
    updated = shipping_store.get_draft(draft_id)
    return updated or {"draft_id": draft_id}


# ── Label streaming ───────────────────────────────────────────────────────────

_SUPPORTED_LABEL_COURIERS = ("inpost", "apaczka", "allegro_delivery")
_MAX_BATCH_LABELS = 100  # provider-agnostic safety cap on one batch print


def _fetch_label_pdf(draft: dict[str, Any], courier: str, storage: Any) -> bytes:
    """Fetch one label PDF for a draft. Shared by the single-label and batch
    endpoints (R5-B).

    Raises :class:`LabelNotReadyError` (HTTP 409) when the label is not printable
    yet — either the draft has no courier id, or InPost rejects the fetch with a
    business error (almost always "shipment not confirmed/processed yet"). Other
    courier failures surface as HTTP 502.
    """
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
            try:
                return InPostClient(token, org_id).get_label(label_id)
            except InPostBusinessError as exc:
                # A business rejection while fetching a label means the shipment
                # is not confirmed/processed yet → not ready, not a hard failure.
                raise LabelNotReadyError(str(exc), courier="inpost", action="get_label") from exc
        elif courier == "apaczka":
            from zdrovena.common.apaczka import ApaczkaClient

            app_id = get_secret("apaczka_app_id")
            app_secret = get_secret("apaczka_app_secret")
            service_id = draft.get("apaczka_service_id") or ""
            return ApaczkaClient(app_id, app_secret, service_id, storage).get_label(label_id)
        else:  # allegro_delivery
            client = _get_allegro_client()
            if client is None:
                raise HTTPException(status_code=502, detail="Allegro credentials missing")
            try:
                return client.get_ship_with_allegro_label(str(label_id))
            except (AllegroBusinessError, AllegroAuthError, CourierTransientError) as exc:
                logger.exception("Allegro label fetch failed for draft %s", draft.get("id"))
                raise HTTPException(status_code=502, detail=f"Allegro API error: {exc}") from exc
    except (HTTPException, ZdrovenaShippingError):
        raise
    except Exception as exc:
        logger.exception("Label fetch failed for draft %s", draft.get("id"))
        raise HTTPException(status_code=502, detail=f"Courier API error: {exc}") from exc


def _merge_pdfs(pdfs: list[bytes]) -> bytes:
    """Merge label PDFs into a single document (R5-B batch printing)."""
    from pypdf import PdfWriter

    writer = PdfWriter()
    for pdf in pdfs:
        writer.append(io.BytesIO(pdf))
    out = io.BytesIO()
    writer.write(out)
    writer.close()
    return out.getvalue()


@router.post(
    "/shipping/labels/batch",
    summary="Fetch and merge labels for several drafts into one printable PDF",
    responses={
        400: {"description": "No draft_ids, too many, or unsupported courier"},
        404: {"description": "None of the drafts exist"},
        409: {"description": "One or more labels are not ready yet"},
    },
)
def batch_labels(
    shipping_store: ShippingStoreDep,
    storage: StorageDep,
    principal: Annotated[Principal, Depends(require_viewer_or_above)],
    draft_ids: Annotated[list[str], Body(embed=True)],
) -> StreamingResponse:
    """Merge the labels of the given drafts into one PDF (R5-B).

    Drafts are grouped by courier (each fetched via the same path as the single
    label endpoint), then concatenated in the request order. Fails deterministically:
      * empty / oversized ``draft_ids`` → 400
      * a not-yet-ready label → 409 listing the offending drafts
      * an unknown draft id → 404 listing them
    """
    if not draft_ids:
        raise HTTPException(status_code=400, detail="draft_ids must not be empty")
    if len(draft_ids) > _MAX_BATCH_LABELS:
        raise HTTPException(
            status_code=400,
            detail=f"Too many labels in one batch (max {_MAX_BATCH_LABELS}, got {len(draft_ids)})",
        )

    drafts: list[dict[str, Any]] = []
    missing: list[str] = []
    for did in draft_ids:
        d = shipping_store.get_draft(did)
        if d is None:
            missing.append(did)
        else:
            drafts.append(d)
    if missing:
        raise HTTPException(status_code=404, detail=f"Draft(s) not found: {', '.join(missing)}")

    bad_courier = [d.get("id") for d in drafts if d.get("courier") not in _SUPPORTED_LABEL_COURIERS]
    if bad_courier:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported courier for draft(s): {', '.join(map(str, bad_courier))}",
        )

    # Group by courier so a future provider bulk-label API can be slotted in per
    # group; today we fetch each label and merge. Order within the response
    # follows the original draft_ids order for predictable printing.
    pdfs: list[bytes] = []
    not_ready: list[str] = []
    for d in drafts:
        try:
            pdfs.append(_fetch_label_pdf(d, d["courier"], storage))
        except LabelNotReadyError:
            not_ready.append(str(d.get("id")))
        except HTTPException as exc:
            # A missing courier id (404) means the draft exists but has no label
            # yet — for a batch that is just another "not ready" case, not a hard
            # failure. Any other courier error (502) aborts the whole batch.
            if exc.status_code == 404:
                not_ready.append(str(d.get("id")))
            else:
                raise
    if not_ready:
        raise HTTPException(
            status_code=409,
            detail=f"Etykiety nie są jeszcze gotowe dla: {', '.join(not_ready)}",
        )

    merged = _merge_pdfs(pdfs)
    return StreamingResponse(
        io.BytesIO(merged),
        media_type="application/pdf",
        headers={"Content-Disposition": 'inline; filename="labels_batch.pdf"'},
    )


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

    pdf_bytes = _fetch_label_pdf(draft, courier, storage)

    order_num = draft.get("shopify_order_number", draft_id).lstrip("#")
    filename = f"label_{courier}_{order_num}.pdf"
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


# ── Allegro invoice (manual) ──────────────────────────────────────────────────


def _get_fakturownia_invoice_client() -> Any | None:
    """Build zdrovena.common.fakturownia.FakturowniaClient for invoice CRUD.

    Distinct from _get_fakturownia_client() which returns the audit-only
    common.client.FakturowniaClient (paginated date-range fetch only).
    """
    from zdrovena.common.config import DEFAULT_DOMAIN, KEYCHAIN_SERVICE_FAKTUROWNIA
    from zdrovena.common.exceptions import MissingSecretError

    try:
        token = get_secret(KEYCHAIN_SERVICE_FAKTUROWNIA)
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

    allegro_client = _get_allegro_client()
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

    allegro_client = _get_allegro_client()
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


@router.post(
    "/shipping/sync",
    status_code=status.HTTP_200_OK,
    summary="Manually trigger order sync from Allegro and Shopify",
)
def sync_orders(
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
    shipping_store: ShippingStoreDep,
    storage: StorageDep,
) -> dict[str, Any]:
    result: dict[str, Any] = {"allegro": None, "shopify": None}

    allegro_client = _get_allegro_client()
    if allegro_client is not None:
        try:
            from zdrovena.api.routers.allegro_poller import poll_orders_once

            fakturownia_client = _get_fakturownia_invoice_client()
            result["allegro"] = poll_orders_once(
                client=allegro_client,
                shipping_store=shipping_store,
                storage=storage,
                fakturownia_client=fakturownia_client,
                fulfillment_status=None,
                retry_existing_invoices=False,
            )
        except Exception as exc:
            logger.exception("Allegro sync failed: %s", exc)
            result["allegro"] = {"error": str(exc)}
    else:
        result["allegro"] = {"error": "credentials_not_configured"}

    shopify_token = get_secret("shopify_admin_token", required=False)
    allowed_domains = _allowed_shopify_domains()
    if shopify_token and allowed_domains:
        shop_domain = next(iter(allowed_domains))
        try:
            result["shopify"] = _sync_shopify_orders_from_api(
                shop_domain=shop_domain,
                api_token=shopify_token,
                shipping_store=shipping_store,
                storage=storage,
            )
        except Exception as exc:
            logger.exception("Shopify sync failed: %s", exc)
            result["shopify"] = {"error": str(exc)}
    else:
        result["shopify"] = {"skipped": "not_configured"}

    log_event(
        "sync.completed",
        actor=getattr(principal, "email", None),
        allegro=result["allegro"],
        shopify=result["shopify"],
    )
    return result
