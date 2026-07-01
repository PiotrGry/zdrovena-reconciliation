"""Map an Allegro checkout-form payload to a Shopify-like order dict.

The Allegro poller reuses ``_create_draft()`` from the webhooks router which
was built for Shopify. Rather than duplicate that entire ~90-line function
(package calc, address parse, phone normalisation, breakdown, etc.), we
project the Allegro payload into the same shape.

We produce only the fields ``_create_draft`` actually reads.
"""

from __future__ import annotations

from typing import Any


def _build_shipping_title(delivery: dict[str, Any]) -> str:
    """Compose a Shopify-like shipping-lines title from Allegro delivery block.

    ``_pick_courier`` looks for 'inpost' / 'paczkomat' in the lowered title.
    ``_pick_inpost_service`` looks for 'paczkomat' vs 'kurier'.
    ``extract_locker_id_from_title`` looks for a locker code inside the title.
    """
    method_name = ((delivery.get("method") or {}).get("name") or "").strip()
    pickup = delivery.get("pickupPoint") or {}
    locker_id = (pickup.get("id") or "").strip()
    if locker_id:
        # Append the locker id so extract_locker_id_from_title can find it.
        return f"{method_name} ({locker_id})".strip()
    return method_name


def allegro_to_shopify_order(form: dict[str, Any]) -> dict[str, Any]:
    """Return a dict shaped like a Shopify orders webhook payload."""
    order_id = form.get("id") or ""
    buyer = form.get("buyer") or {}
    delivery = form.get("delivery") or {}
    address = delivery.get("address") or {}
    pickup = delivery.get("pickupPoint") or {}

    first_name = address.get("firstName") or buyer.get("firstName") or ""
    last_name = address.get("lastName") or buyer.get("lastName") or ""
    phone = address.get("phoneNumber") or buyer.get("phoneNumber") or ""
    email = buyer.get("email") or ""

    shipping_title = _build_shipping_title(delivery)

    line_items: list[dict[str, Any]] = []
    for li in form.get("lineItems") or []:
        offer = li.get("offer") or {}
        external = offer.get("external") or {}
        sku = external.get("id") or offer.get("id") or ""
        line_items.append(
            {
                "name": offer.get("name", ""),
                "title": offer.get("name", ""),
                "quantity": int(li.get("quantity", 1) or 1),
                "sku": sku,
            }
        )

    method_id = ((delivery.get("method") or {}).get("id") or "").strip()

    note_attributes: list[dict[str, str]] = []
    if pickup.get("id"):
        note_attributes.append({"name": "PickupPointId", "value": pickup["id"]})
        if pickup.get("name"):
            note_attributes.append({"name": "PickupPointName", "value": pickup["name"]})
    if method_id:
        # Consumed by _create_draft to enable Ship with Allegro (create-commands).
        note_attributes.append({"name": "AllegroDeliveryMethodId", "value": method_id})

    order: dict[str, Any] = {
        "id": order_id,
        "order_number": order_id,  # display value; Allegro id is a UUID-ish string
        "email": email,
        "phone": phone,
        "customer": {
            "first_name": buyer.get("firstName", ""),
            "last_name": buyer.get("lastName", ""),
            "email": email,
            "phone": buyer.get("phoneNumber", ""),
        },
        "shipping_address": {
            "first_name": first_name,
            "last_name": last_name,
            "name": f"{first_name} {last_name}".strip(),
            "address1": address.get("street", ""),
            "address2": pickup.get("id", "") or "",
            "city": address.get("city", ""),
            "zip": address.get("zipCode", ""),
            "country": "Poland",
            "country_code": address.get("countryCode", "PL"),
            "phone": phone,
        },
        "shipping_lines": [{"title": shipping_title}],
        "line_items": line_items,
        "note_attributes": note_attributes,
    }
    return order
