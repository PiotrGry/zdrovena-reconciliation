"""HTTP-neutral Apaczka parcel and payload planning."""

from __future__ import annotations

from typing import Any, Protocol

from zdrovena.common.shipping_exceptions import ApaczkaBusinessError
from zdrovena.common.shipping_parcels import PARCEL_SPECS
from zdrovena.shipping.domain.planning import (
    parcel_content,
    physical_parcels,
    shipment_reference,
)

ApaczkaCallSpec = dict[str, Any]

APACZKA_PICKUP_WINDOWS = (
    ("09:00", "17:00"),
    ("11:00", "14:00"),
    ("14:00", "17:00"),
)
APACZKA_FIXED_WINDOW_SERVICE_IDS = frozenset({"23"})
APACZKA_POINT_TYPE_BY_SERVICE_ID = {
    "14": "UPS",
    "15": "UPS",
    "23": "DPD",
    "26": "DPD",
    "50": "PWR",
    "53": "PWR",
    "64": "POCZTA",
    "66": "POCZTA",
    "86": "DHL_PARCEL",
    "203": "GLS",
    "314": "PACKETA",
    "317": "PACKETA",
}


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
        cod_amount: str | None = None,
        cod_currency: str = "PLN",
        cod_bank_account: str | None = None,
    ) -> dict[str, Any]: ...


class ApaczkaPointLookup(Protocol):
    """Read-only point capability used before an Apaczka provider write."""

    def get_point(self, point_type: str, foreign_address_id: str) -> dict[str, Any] | None: ...


def apaczka_cod_pickup_point_id(draft: dict[str, Any]) -> str | None:
    """Return the selected point only when this draft actually requires COD validation."""
    if not draft.get("cod"):
        return None
    receiver = draft.get("receiver") or {}
    pickup_point = draft.get("pickup_point") or {}
    point_id = str(pickup_point.get("id") or receiver.get("locker_id") or "").strip()
    return point_id or None


def validate_apaczka_cod_pickup_point(
    draft: dict[str, Any], point_lookup: ApaczkaPointLookup
) -> None:
    """Fail closed unless the selected Apaczka point explicitly advertises COD."""
    point_id = apaczka_cod_pickup_point_id(draft)
    if point_id is None:
        return
    service_id = str(draft.get("apaczka_service_id") or "").strip()
    point_type = APACZKA_POINT_TYPE_BY_SERVICE_ID.get(service_id)
    if point_type is None:
        raise ApaczkaBusinessError(
            f"Cannot verify COD capability for Apaczka point {point_id}: "
            f"service {service_id or '?'} has no documented point type",
            courier="apaczka",
            action="validate_point_cod",
        )
    point = point_lookup.get_point(point_type, point_id)
    if point is None:
        raise ApaczkaBusinessError(
            f"Cannot verify COD capability: Apaczka point {point_id} was not found",
            courier="apaczka",
            action="validate_point_cod",
        )
    if point.get("option_cod") is not True:
        raise ApaczkaBusinessError(
            f"Apaczka point {point_id} does not support COD",
            courier="apaczka",
            action="validate_point_cod",
        )


def apaczka_call_specs(
    draft: dict[str, Any],
    pickup_address: dict[str, str],
    *,
    cod_bank_account: str | None = None,
    pickup_date: str | None = None,
    pickup_from: str | None = None,
    pickup_to: str | None = None,
) -> list[ApaczkaCallSpec]:
    """Return builder arguments for physical parcels not already checkpointed."""
    if draft.get("cod_error"):
        raise ApaczkaBusinessError(
            f"Invalid Shopify COD data: {draft['cod_error']}",
            courier="apaczka",
            action="create_shipment",
        )
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

    existing_keys = {
        (str(item.get("package_type")), int(item.get("package_number") or 1))
        for item in draft.get("courier_shipments") or []
    }

    parcels = physical_parcels(draft)
    cod = draft.get("cod")
    if cod and len(parcels) != 1:
        raise ApaczkaBusinessError(
            "COD requires exactly one physical Apaczka shipment; multi-parcel COD is blocked",
            courier="apaczka",
            action="create_shipment",
        )

    specs: list[ApaczkaCallSpec] = []
    for parcel in parcels:
        package_type = parcel.package_type
        package_number = parcel.position
        package_count = parcel.count_for_type
        if (package_type, package_number) in existing_keys:
            continue
        spec = PARCEL_SPECS.get(package_type, PARCEL_SPECS["1-pak"])
        kwargs: dict[str, Any] = {
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
            "content": parcel_content(package_type),
            "weight_kg": spec["weight_kg"],
            "width_cm": spec["width"],
            "height_cm": spec["height"],
            "depth_cm": spec["length"],
            "pickup_date": pickup_date,
            "pickup_from": pickup_from,
            "pickup_to": pickup_to,
        }
        if cod:
            kwargs.update(
                {
                    "cod_amount": str(cod.get("amount") or ""),
                    "cod_currency": str(cod.get("currency") or ""),
                    "cod_bank_account": cod_bank_account,
                }
            )
        specs.append(
            {
                "package_type": package_type,
                "package_number": package_number,
                "kwargs": kwargs,
            }
        )
    return specs


def apaczka_payload_plan(
    draft: dict[str, Any],
    pickup_address: dict[str, str],
    builder: ApaczkaPayloadBuilder,
    *,
    cod_bank_account: str | None = None,
    pickup_date: str | None = None,
    pickup_from: str | None = None,
    pickup_to: str | None = None,
) -> list[dict[str, Any]]:
    """Return the exact Apaczka payloads without sending them."""
    call_specs = apaczka_call_specs(
        draft,
        pickup_address,
        cod_bank_account=cod_bank_account,
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
