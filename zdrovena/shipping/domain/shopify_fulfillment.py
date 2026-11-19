"""Shopify fulfillment planning: which lines ship, which carrier, who gets told.

Pure and HTTP-free so every rule here is unit-tested without a fake Shopify.
The composition layer (``zdrovena.api.shopify_fulfillment_composition``) does
the HTTP and persistence around it.

Two rules live here, each in exactly one place (#541):

* :func:`is_deposit_line` — a Shopify line item is the bottle deposit
  ("kaucja"), not water. Deposit lines are fulfilled silently so the customer
  receives one shipping email listing only the water.
* :data:`SHOPIFY_CARRIER_TABLE` / :func:`shopify_tracking_carrier` — the real
  carrier of a draft mapped to a name from Shopify's supported carrier list,
  so the fulfillment shows that carrier selected rather than "Other".
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

# ── deposit rule ─────────────────────────────────────────────────────────────

_DEPOSIT_TITLE_MARKER = "kaucja"
_LINE_TITLE_FIELDS = ("title", "name", "variant_title")


def is_deposit_line(line_item: Mapping[str, Any]) -> bool:
    """True when a Shopify order line item is the deposit (kaucja), not a product.

    A line is a deposit when its title contains ``kaucja`` (case-insensitive) or
    Shopify says it does not require shipping (``requires_shipping is False``).
    A missing ``requires_shipping`` is not read as False: only an explicit
    "nothing to ship" counts.
    """
    title = " ".join(str(line_item.get(field) or "") for field in _LINE_TITLE_FIELDS)
    if _DEPOSIT_TITLE_MARKER in title.casefold():
        return True
    return line_item.get("requires_shipping") is False


# ── carrier table ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ShopifyCarrier:
    """A carrier as Shopify knows it, plus a public tracking page."""

    company: str
    tracking_url_template: str

    def tracking_url(self, tracking_number: str) -> str:
        return self.tracking_url_template.format(number=tracking_number)


INPOST = ShopifyCarrier("InPost", "https://inpost.pl/sledzenie-przesylek?number={number}")
UPS = ShopifyCarrier("UPS", "https://www.ups.com/track?loc=pl_PL&tracknum={number}")
DPD = ShopifyCarrier("DPD", "https://tracktrace.dpd.com.pl/parcelDetails?typ=1&p1={number}")
DHL_PARCEL = ShopifyCarrier(
    "DHL Parcel",
    "https://www.dhl.com/pl-pl/home/tracking/tracking-parcel.html?submit=1&tracking-id={number}",
)
POCZTA_POLSKA = ShopifyCarrier(
    "Poczta Polska", "https://emonitoring.poczta-polska.pl/?numer={number}"
)
FEDEX = ShopifyCarrier("FedEx", "https://www.fedex.com/fedextrack/?trknbr={number}")
GLS = ShopifyCarrier("GLS", "https://gls-group.com/PL/pl/sledzenie-paczek?match={number}")
ORLEN_PACZKA = ShopifyCarrier(
    "Orlen Paczka", "https://www.orlenpaczka.pl/sledz-paczke/?numer={number}"
)
PACKETA = ShopifyCarrier("Packeta", "https://tracking.packeta.com/pl/?id={number}")

# Universal tracker for a carrier outside the table: Shopify gets the carrier's
# own name (shown as "Other") and a link that still resolves the number.
UNKNOWN_CARRIER_TRACKING_URL = "https://t.17track.net/pl#nums={number}"

# The one table. Keys are ``(courier, carrier_key)``:
#   * ``("inpost", "")`` — InPost ShipX;
#   * ``("apaczka", service_id)`` — every id in ``APACZKA_SERVICE_CATALOG``
#     (``zdrovena.common.apaczka``; a test keeps the two in step);
#   * ``("allegro_delivery", carrierId)`` — the carrier Allegro reports for
#     the shipment (``transportingInfo[].carrierId``).
SHOPIFY_CARRIER_TABLE: dict[tuple[str, str], ShopifyCarrier] = {
    ("inpost", ""): INPOST,
    # Apaczka — door-to-door
    ("apaczka", "1"): UPS,
    ("apaczka", "2"): UPS,
    ("apaczka", "3"): UPS,
    ("apaczka", "4"): UPS,
    ("apaczka", "21"): DPD,
    ("apaczka", "24"): DPD,
    ("apaczka", "25"): DPD,
    ("apaczka", "60"): POCZTA_POLSKA,
    ("apaczka", "82"): DHL_PARCEL,
    ("apaczka", "83"): DHL_PARCEL,
    ("apaczka", "84"): DHL_PARCEL,
    ("apaczka", "151"): FEDEX,
    ("apaczka", "202"): GLS,
    # Apaczka — point / locker
    ("apaczka", "14"): UPS,
    ("apaczka", "15"): UPS,
    ("apaczka", "23"): DPD,
    ("apaczka", "26"): DPD,
    ("apaczka", "50"): ORLEN_PACZKA,
    ("apaczka", "53"): ORLEN_PACZKA,
    ("apaczka", "64"): POCZTA_POLSKA,
    ("apaczka", "66"): POCZTA_POLSKA,
    ("apaczka", "86"): DHL_PARCEL,
    ("apaczka", "203"): GLS,
    ("apaczka", "314"): PACKETA,
    ("apaczka", "317"): PACKETA,
    # Allegro Delivery — the carrier actually moving the parcel
    ("allegro_delivery", "INPOST"): INPOST,
    ("allegro_delivery", "DPD"): DPD,
    ("allegro_delivery", "UPS"): UPS,
    ("allegro_delivery", "DHL"): DHL_PARCEL,
    ("allegro_delivery", "POCZTA_POLSKA"): POCZTA_POLSKA,
    ("allegro_delivery", "ORLEN"): ORLEN_PACZKA,
    ("allegro_delivery", "GLS"): GLS,
    ("allegro_delivery", "FEDEX"): FEDEX,
}

_UNKNOWN_CARRIER_NAMES = {
    "inpost": "InPost",
    "apaczka": "Apaczka",
    "allegro_delivery": "Allegro Delivery",
    "allegro": "Allegro Delivery",
}


def _carrier_key(draft: Mapping[str, Any]) -> tuple[str, str]:
    courier = str(draft.get("courier") or "").strip().lower()
    if courier == "allegro":
        courier = "allegro_delivery"
    if courier == "apaczka":
        return courier, str(draft.get("apaczka_service_id") or "").strip()
    if courier == "allegro_delivery":
        return courier, str(draft.get("allegro_carrier_id") or "").strip().upper()
    return courier, ""


def shopify_tracking_carrier(draft: Mapping[str, Any]) -> ShopifyCarrier | None:
    """Shopify-supported carrier for a draft, or ``None`` when it is not in the table."""
    return SHOPIFY_CARRIER_TABLE.get(_carrier_key(draft))


def tracking_info(draft: Mapping[str, Any], tracking_number: str) -> dict[str, str]:
    """``tracking_info`` for the notifying fulfillment.

    A carrier in the table is sent under its Shopify name with its tracking
    page. Anything else keeps a readable name (the Allegro carrier id or the
    courier) and a universal tracking link, so the
    customer's email still has a working link.
    """
    carrier = shopify_tracking_carrier(draft)
    if carrier is not None:
        return {
            "number": tracking_number,
            "company": carrier.company,
            "url": carrier.tracking_url(tracking_number),
        }
    return {
        "number": tracking_number,
        "company": _unknown_carrier_name(draft),
        "url": UNKNOWN_CARRIER_TRACKING_URL.format(number=tracking_number),
    }


def _unknown_carrier_name(draft: Mapping[str, Any]) -> str:
    courier, key = _carrier_key(draft)
    if courier == "allegro_delivery" and key:
        return key.replace("_", " ").title()
    return _UNKNOWN_CARRIER_NAMES.get(courier) or str(draft.get("courier") or "Other")


# ── fulfillment plan ─────────────────────────────────────────────────────────

FULFILLABLE_FULFILLMENT_ORDER_STATUSES = frozenset({"open", "in_progress"})


@dataclass(frozen=True)
class PlannedFulfillment:
    """One ``POST /fulfillments.json`` to make."""

    kind: str  # "products" | "deposit" | "all"
    notify_customer: bool
    with_tracking: bool
    line_items_by_fulfillment_order: list[dict[str, Any]]

    def payload(self, tracking: dict[str, str] | None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "line_items_by_fulfillment_order": self.line_items_by_fulfillment_order,
            "notify_customer": self.notify_customer,
        }
        if self.with_tracking and tracking:
            body["tracking_info"] = tracking
        return {"fulfillment": body}


def open_fulfillment_order_lines(
    fulfillment_orders: Iterable[Mapping[str, Any]],
) -> list[tuple[Any, Mapping[str, Any]]]:
    """``(fulfillment_order_id, line)`` for every line Shopify still lets us fulfill."""
    lines: list[tuple[Any, Mapping[str, Any]]] = []
    for fulfillment_order in fulfillment_orders:
        if fulfillment_order.get("status") not in FULFILLABLE_FULFILLMENT_ORDER_STATUSES:
            continue
        for line in fulfillment_order.get("line_items") or []:
            if int(line.get("fulfillable_quantity") or 0) > 0:
                lines.append((fulfillment_order["id"], line))
    return lines


def _group(lines: list[tuple[Any, Mapping[str, Any]]]) -> list[dict[str, Any]]:
    grouped: dict[Any, list[dict[str, Any]]] = {}
    for fulfillment_order_id, line in lines:
        grouped.setdefault(fulfillment_order_id, []).append(
            {"id": line["id"], "quantity": int(line["fulfillable_quantity"])}
        )
    return [
        {"fulfillment_order_id": fulfillment_order_id, "fulfillment_order_line_items": items}
        for fulfillment_order_id, items in grouped.items()
    ]


def plan_fulfillments(
    fulfillment_orders: Iterable[Mapping[str, Any]],
    order_line_items: Iterable[Mapping[str, Any]] | None,
) -> list[PlannedFulfillment]:
    """Split the open fulfillment-order lines into the fulfillments to create.

    * water (product) lines — one fulfillment that notifies the customer and
      carries the tracking number;
    * deposit lines — one silent fulfillment without tracking.

    When a line cannot be classified (the order's line items are unknown or do
    not include it) the deposit cannot be told apart from the water, so
    everything goes into one *silent* fulfillment with tracking: better no
    email than an email that lists the deposit as a shipped item.

    Empty result: Shopify has nothing left to fulfill.
    """
    lines = open_fulfillment_order_lines(fulfillment_orders)
    if not lines:
        return []

    by_line_item_id = {
        str(item.get("id")): item for item in (order_line_items or []) if item.get("id")
    }
    products: list[tuple[Any, Mapping[str, Any]]] = []
    deposits: list[tuple[Any, Mapping[str, Any]]] = []
    for fulfillment_order_id, line in lines:
        order_line = by_line_item_id.get(str(line.get("line_item_id")))
        if order_line is None:
            return [
                PlannedFulfillment(
                    kind="all",
                    notify_customer=False,
                    with_tracking=True,
                    line_items_by_fulfillment_order=_group(lines),
                )
            ]
        target = deposits if is_deposit_line(order_line) else products
        target.append((fulfillment_order_id, line))

    plan: list[PlannedFulfillment] = []
    if products:
        plan.append(
            PlannedFulfillment(
                kind="products",
                notify_customer=True,
                with_tracking=True,
                line_items_by_fulfillment_order=_group(products),
            )
        )
    if deposits:
        plan.append(
            PlannedFulfillment(
                kind="deposit",
                notify_customer=False,
                with_tracking=False,
                line_items_by_fulfillment_order=_group(deposits),
            )
        )
    return plan
