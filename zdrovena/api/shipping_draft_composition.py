"""API composition for mapping external orders into shipping drafts.

This module owns external payload/configuration interpretation and binds the
HTTP-neutral draft application workflow to API effects. It intentionally does
not belong to ``shipping.application``: Shopify/Allegro payload shapes, secret
lookup, SMS delivery, and environment title maps are integration concerns.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Any

from zdrovena.api.observability import correlation_scope
from zdrovena.audit.bottles import SKIP_RE
from zdrovena.common.events import log_event
from zdrovena.common.secrets import get_secret
from zdrovena.common.shipping_format import (
    extract_locker_id_from_title,
    normalize_pl_phone,
    parse_pl_address,
)
from zdrovena.common.shipping_store import ShippingStore
from zdrovena.shipping.application import drafts as draft_application
from zdrovena.shipping.domain.planning import calc_packages, package_fit_warnings

logger = logging.getLogger("zdrovena.api.shipping_draft_composition")


def emit_tracking_assigned(draft_id: Any, order_number: Any, origin: str) -> None:
    """Emit the shared audit event whenever a draft gains tracking."""
    log_event(
        "draft.tracking_assigned",
        draft_id=draft_id,
        order_number=order_number,
        shipment_origin=origin,
    )


def maybe_send_new_order_sms(draft: dict[str, Any]) -> None:
    """Send the optional operator notification for a newly composed draft."""
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


def _parse_title_map(raw: str) -> dict[str, str]:
    """Parse an environment title map in JSON or ``key=value`` format."""
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
            str(key).strip().lower(): str(value).strip()
            for key, value in parsed.items()
            if str(key).strip() and str(value).strip()
        }
    result: dict[str, str] = {}
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
    return _parse_title_map(os.getenv("COURIER_TITLE_MAP", ""))


@lru_cache(maxsize=1)
def _inpost_service_title_map() -> dict[str, str]:
    return _parse_title_map(os.getenv("INPOST_SERVICE_TITLE_MAP", ""))


@lru_cache(maxsize=1)
def _apaczka_service_title_map() -> dict[str, str]:
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
    "dpd": "23",
    "poczta": "64",
}
_APACZKA_SERVICES_REQUIRING_PICKUP_POINT = frozenset(_APACZKA_PICKUP_SERVICES.values())
_SHOPIFY_COD_GATEWAYS = frozenset({"cash on delivery (cod)"})


def _shopify_cod_details(
    order: dict[str, Any],
) -> tuple[dict[str, str] | None, str | None]:
    """Return the exact outstanding COD amount, or a fail-closed review reason.

    Shopify's financial status alone is not a COD signal: card payments can also
    be pending.  Order #1706 and Shopify's documented contract both identify COD
    through ``payment_gateway_names``.  ``total_outstanding`` is the amount still
    owed after partial payments/edits, so using ``total_price`` could overcharge.
    """
    gateways = {
        " ".join(str(value or "").strip().lower().split())
        for value in [*(order.get("payment_gateway_names") or []), order.get("gateway")]
        if value
    }
    matched_gateway = next((value for value in gateways if value in _SHOPIFY_COD_GATEWAYS), None)
    if matched_gateway is None:
        return None, None

    raw_amount = order.get("total_outstanding")
    if raw_amount is None or str(raw_amount).strip() == "":
        return None, "COD order is missing Shopify total_outstanding"
    try:
        amount = Decimal(str(raw_amount))
    except (InvalidOperation, ValueError):
        return None, f"Invalid Shopify COD total_outstanding: {raw_amount!r}"
    if not amount.is_finite() or amount < 0:
        return None, f"Invalid Shopify COD total_outstanding: {raw_amount!r}"
    if amount == 0:
        # A COD gateway can remain on a manually-paid order. There is no money
        # left for the courier to collect in that case.
        return None, None
    if amount != amount.quantize(Decimal("0.01")):
        return None, f"Shopify COD total_outstanding has sub-cent precision: {raw_amount!r}"

    currency = str(order.get("currency") or "").strip().upper()
    if currency != "PLN":
        return None, f"Unsupported COD currency: {currency or '<missing>'}"
    return {
        "amount": format(amount, ".2f"),
        "currency": currency,
        "gateway": matched_gateway,
    }, None


def _normalize_pickup_provider(value: Any) -> str | None:
    normalized = " ".join(str(value or "").strip().lower().split())
    return _PICKUP_PROVIDER_ALIASES.get(normalized)


def _extract_shopify_pickup_point(order: dict[str, Any]) -> dict[str, str] | None:
    """Extract trusted Octolize pickup-point metadata from a Shopify order."""
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


def reset_courier_maps_cache() -> None:
    """Clear cached environment mappings (used by configuration tests)."""
    _courier_title_map.cache_clear()
    _inpost_service_title_map.cache_clear()
    _apaczka_service_title_map.cache_clear()


_MATCH_AUTO = "auto_matched"
_MATCH_REQUIRES_SELECTION = "requires_selection"
_MATCH_UNRECOGNIZED = "unrecognized"


def pick_courier(order: dict[str, Any]) -> str:
    """Map an external shipping title to a concrete courier."""
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


def pick_inpost_service(title: str) -> str:
    lowered = title.lower()
    explicit = _inpost_service_title_map()
    if explicit:
        for keyword, service in explicit.items():
            if keyword and keyword in lowered:
                return service
    return "paczkomat" if "paczkomat" in lowered else "kurier"


def pick_apaczka_service(title: str) -> str | None:
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


def build_draft_record(
    order: dict[str, Any],
    *,
    source: str = "shopify",
    draft_id: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Map one Shopify-shaped external order into the persisted draft record."""
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

    note_attrs = {attr["name"]: attr["value"] for attr in (order.get("note_attributes") or [])}
    pickup_point = _extract_shopify_pickup_point(order)
    allegro_method_id = (note_attrs.get("AllegroDeliveryMethodId") or "").strip()
    use_allegro_delivery = source == "allegro" and bool(allegro_method_id)

    if use_allegro_delivery:
        courier = "allegro_delivery"
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
            courier = pick_courier(order)
            inpost_service = pick_inpost_service(title) if courier == "inpost" else None
            apaczka_service_id = pick_apaczka_service(title) if courier == "apaczka" else None

    line_items = order.get("line_items") or []
    product_items = [item for item in line_items if not SKIP_RE.search(item.get("name", ""))]
    total_qty = max(sum(item.get("quantity", 1) for item in product_items), 1)
    package_plan = calc_packages(product_items)
    packages_count, packages_breakdown = package_plan.to_legacy_tuple()
    cod, cod_error = _shopify_cod_details(order) if source == "shopify" else (None, None)
    for warning in package_fit_warnings(packages_breakdown, carrier="inpost"):
        logger.warning("_calc_packages: %s", warning)
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

    street, building_number = parse_pl_address(shipping_addr.get("address1", ""))
    phone = normalize_pl_phone(phone) if phone else phone
    needs_review = (
        phone is None
        or (courier == "apaczka" and apaczka_service_id is None)
        or cod_error is not None
        or (cod is not None and packages_count != 1)
    )
    if (
        courier == "apaczka"
        and apaczka_service_id in _APACZKA_SERVICES_REQUIRING_PICKUP_POINT
        and not (pickup_point or {}).get("id")
    ):
        needs_review = True

    source_fulfillment = draft_application.source_fulfillment_status(order, source=source)
    now = datetime.now(timezone.utc).isoformat()
    base_status = "needs_review" if needs_review else "pending"
    fulfillment_details = (
        draft_application.source_fulfillment_details(order) if source == "shopify" else {}
    )
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
        "courier_shipments": [],
        "dispatch_order_id": None,
        "status": draft_application.status_from_source(order, base_status, source=source),
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
            "building_number": building_number,
            "flat_number": shipping_addr.get("address2", ""),
            "city": shipping_addr.get("city", ""),
            "post_code": shipping_addr.get("zip", ""),
        },
        "parcel": {"template": "large", "weight_kg": None},
        "cod": cod,
        "cod_error": cod_error,
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
    if courier == "allegro_delivery":
        record["allegro_delivery_method_id"] = allegro_method_id
        record["allegro_credentials_id"] = None
        record["allegro_sending_method"] = allegro_sending_method
    return record


def create_draft(
    order: dict[str, Any],
    shipping_store: ShippingStore,
    *,
    source: str = "shopify",
) -> dict[str, Any]:
    """Create a draft with the API-owned mapper and effects."""
    return draft_application.create_draft(
        order,
        shipping_store,
        build_draft_record=build_draft_record,
        emit_tracking_assigned=emit_tracking_assigned,
        record_event=log_event,
        send_new_order_sms=maybe_send_new_order_sms,
        source=source,
    )


def create_draft_safely(
    order: dict[str, Any],
    shipping_store: ShippingStore,
    *,
    source: str = "shopify",
    correlation_id: str = "-",
) -> None:
    """Create one draft and persist any background failure to the DLQ."""
    with correlation_scope(correlation_id):
        try:
            create_draft(order, shipping_store, source=source)
        except Exception as exc:
            logger.exception(
                "Draft creation failed for order %s (source=%s) — enqueueing to DLQ",
                order.get("id") or order.get("order_number"),
                source,
            )
            try:
                entry = shipping_store.enqueue_dlq(
                    payload=order,
                    error=f"{type(exc).__name__}: {exc}",
                    source=source,
                )
                log_event(
                    "dlq.enqueued",
                    level=logging.ERROR,
                    entry_id=entry["id"],
                    order_number=order.get("order_number") or order.get("id"),
                    source=source,
                    error_type=type(exc).__name__,
                    test_probe=False,
                )
            except Exception:
                logger.exception(
                    "DLQ enqueue itself failed for order %s",
                    order.get("id") or order.get("order_number"),
                )
