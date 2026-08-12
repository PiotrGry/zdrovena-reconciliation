"""HTTP-neutral Apaczka parcel and payload planning."""

from __future__ import annotations

from typing import Any, Protocol

from zdrovena.common.shipping_exceptions import ApaczkaBusinessError
from zdrovena.common.shipping_parcels import PARCEL_SPECS
from zdrovena.shipping.domain.planning import physical_parcels, shipment_reference

ApaczkaCallSpec = dict[str, Any]

APACZKA_PICKUP_WINDOWS = (
    ("09:00", "17:00"),
    ("11:00", "14:00"),
    ("14:00", "17:00"),
)
APACZKA_FIXED_WINDOW_SERVICE_IDS = frozenset({"23"})


def validate_apaczka_pickup_window(
    service_id: str,
    pickup_date: str | None,
    pickup_from: str | None,
    pickup_to: str | None,
) -> None:
    """Reject windows known to be invalid for the verified incident service."""
    if service_id not in APACZKA_FIXED_WINDOW_SERVICE_IDS:
        return
    if pickup_date is None and pickup_from is None and pickup_to is None:
        return
    window = (pickup_from or "", pickup_to or "")
    if not pickup_date or window not in APACZKA_PICKUP_WINDOWS:
        allowed = ", ".join(f"{start}–{end}" for start, end in APACZKA_PICKUP_WINDOWS)
        requested = f"{pickup_from or '?'}–{pickup_to or '?'}"
        raise ApaczkaBusinessError(
            f"Unsupported Apaczka pickup window {requested}. Allowed windows: {allowed}",
            courier="apaczka",
            action="validate_pickup_window",
        )


class ApaczkaPayloadBuilder(Protocol):
    """Payload-builder capability used by read-only preview planning."""

    def build_shipment_order(
        self,
        *,
        receiver_name: str,
        receiver_firstname: str,
        receiver_lastname: str,
        receiver_email: str,
        receiver_phone: str,
        receiver_address: str,
        receiver_city: str,
        receiver_zip: str,
        receiver_point_id: str | None = None,
        sender: dict[str, str],
        reference: str,
        content: str,
        weight_kg: float = 1.0,
        width_cm: float = 20.0,
        height_cm: float = 15.0,
        depth_cm: float = 30.0,
        pickup_date: str | None = None,
        pickup_from: str | None = None,
        pickup_to: str | None = None,
    ) -> dict[str, Any]: ...


def apaczka_call_specs(
    draft: dict[str, Any],
    pickup_address: dict[str, str],
    *,
    pickup_date: str | None = None,
    pickup_from: str | None = None,
    pickup_to: str | None = None,
) -> list[ApaczkaCallSpec]:
    """Return builder arguments for physical parcels not already checkpointed."""
    validate_apaczka_pickup_window(
        str(draft.get("apaczka_service_id") or ""),
        pickup_date,
        pickup_from,
        pickup_to,
    )
    receiver = draft.get("receiver") or {}
    pickup_point = draft.get("pickup_point") or {}
    receiver_point_id = str(pickup_point.get("id") or receiver.get("locker_id") or "").strip()
    addr = draft.get("shipping_address") or {}
    customer_name = f"{receiver.get('first_name', '')} {receiver.get('last_name', '')}".strip()

    content_parts: list[str] = []
    for item in draft.get("order_items") or []:
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        quantity = item.get("quantity", 1)
        content_parts.append(f"{quantity} x {name}")
    shipment_content = ", ".join(content_parts)[:255] or "Woda butelkowana"

    existing_keys = {
        (str(item.get("package_type")), int(item.get("package_number") or 1))
        for item in draft.get("courier_shipments") or []
    }

    specs: list[ApaczkaCallSpec] = []
    for parcel in physical_parcels(draft):
        package_type = parcel.package_type
        package_number = parcel.position
        package_count = parcel.count_for_type
        if (package_type, package_number) in existing_keys:
            continue
        spec = PARCEL_SPECS.get(package_type, PARCEL_SPECS["1-pak"])
        specs.append(
            {
                "package_type": package_type,
                "package_number": package_number,
                "kwargs": {
                    "receiver_name": customer_name,
                    "receiver_firstname": receiver.get("first_name", ""),
                    "receiver_lastname": receiver.get("last_name", ""),
                    "receiver_email": receiver.get("email", ""),
                    "receiver_phone": receiver.get("phone", ""),
                    "receiver_address": " ".join(
                        filter(
                            None,
                            [
                                addr.get("street", ""),
                                addr.get("building_number", ""),
                                addr.get("flat_number", ""),
                            ],
                        )
                    ),
                    "receiver_city": addr.get("city", ""),
                    "receiver_zip": addr.get("post_code", ""),
                    "receiver_point_id": receiver_point_id or None,
                    # Deliberate: Apaczka prints the pickup address as sender.
                    "sender": pickup_address,
                    "reference": shipment_reference(
                        str(draft.get("shopify_order_number", "")),
                        package_type,
                        package_number,
                        package_count,
                    ),
                    "content": shipment_content,
                    "weight_kg": spec["weight_kg"],
                    "width_cm": spec["width"],
                    "height_cm": spec["height"],
                    "depth_cm": spec["length"],
                    "pickup_date": pickup_date,
                    "pickup_from": pickup_from,
                    "pickup_to": pickup_to,
                },
            }
        )
    return specs


def apaczka_payload_plan(
    draft: dict[str, Any],
    pickup_address: dict[str, str],
    builder: ApaczkaPayloadBuilder,
    *,
    pickup_date: str | None = None,
    pickup_from: str | None = None,
    pickup_to: str | None = None,
) -> list[dict[str, Any]]:
    """Return the exact Apaczka payloads without sending them."""
    call_specs = apaczka_call_specs(
        draft,
        pickup_address,
        pickup_date=pickup_date,
        pickup_from=pickup_from,
        pickup_to=pickup_to,
    )
    return [
        {
            "service": draft.get("service"),
            "package_type": spec["package_type"],
            "package_number": spec["package_number"],
            "reference": spec["kwargs"]["reference"],
            "payload": builder.build_shipment_order(**spec["kwargs"]),
        }
        for spec in call_specs
    ]


__all__ = [
    "APACZKA_FIXED_WINDOW_SERVICE_IDS",
    "APACZKA_PICKUP_WINDOWS",
    "ApaczkaCallSpec",
    "ApaczkaPayloadBuilder",
    "apaczka_call_specs",
    "apaczka_payload_plan",
    "validate_apaczka_pickup_window",
]
