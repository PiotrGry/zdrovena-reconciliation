"""HTTP-neutral Allegro Delivery shipment planning."""

from __future__ import annotations

from typing import Any, Protocol

from zdrovena.common.shipping_exceptions import AllegroBusinessError
from zdrovena.common.shipping_parcels import PARCEL_SPECS
from zdrovena.shipping.domain.planning import (
    physical_parcels,
    shipment_reference,
)

AllegroCallSpec = dict[str, Any]

# Allegro create-commands enum for the InPost sending mode. Contract per Allegro
# issue #9915: parcel_locker | dispatch_order | pop | any_point. Only sent for
# InPost drafts; other carriers derive the field from the order.
ALLEGRO_INPOST_SENDING_METHODS = frozenset({"parcel_locker", "dispatch_order", "pop", "any_point"})


class AllegroDeliveryProposalClient(Protocol):
    """Read-only Allegro capability required to plan a shipment preview."""

    def get_delivery_proposal(self, order_id: str) -> dict[str, Any]: ...


def allegro_call_spec(draft: dict[str, Any], proposal: dict[str, Any]) -> AllegroCallSpec:
    """Build create-command arguments from a draft and Allegro proposal."""
    # FLAT dimensions, each a {"value", "unit"} object; weight unit is the
    # plural "KILOGRAMS"; type is required.
    parcels = physical_parcels(draft)
    packages = []
    for parcel in parcels:
        spec = PARCEL_SPECS.get(parcel.package_type) or PARCEL_SPECS["1-pak"]
        packages.append(
            {
                "type": "PACKAGE",
                "length": {"value": spec["length"], "unit": "CENTIMETER"},
                "width": {"value": spec["width"], "unit": "CENTIMETER"},
                "height": {"value": spec["height"], "unit": "CENTIMETER"},
                "weight": {"value": round(float(spec["weight_kg"]), 2), "unit": "KILOGRAMS"},
            }
        )

    # Allegro pre-fills both required address blocks under suggestedInput.
    suggested_input = proposal.get("suggestedInput")
    if not isinstance(suggested_input, dict):
        raise AllegroBusinessError(
            detail="Allegro delivery proposal has no suggestedInput object",
            action="get_delivery_proposal",
        )
    sender = suggested_input.get("sender") or {}
    receiver = dict(suggested_input.get("receiver") or {})
    if not sender or not receiver:
        raise AllegroBusinessError(
            detail="Allegro delivery proposal has no suggestedInput.sender or receiver",
            action="get_delivery_proposal",
        )

    # Pickup-point / locker code lives inside the receiver block as `point`.
    pickup_point_id = (draft.get("receiver") or {}).get("locker_id") or None
    if pickup_point_id:
        receiver["point"] = pickup_point_id

    additional_properties: dict[str, Any] | None = None
    sending_method = draft.get("allegro_sending_method")
    if sending_method and sending_method in ALLEGRO_INPOST_SENDING_METHODS:
        additional_properties = {"inpost#sendingMethod": sending_method}

    parcel = parcels[0]
    reference_number = shipment_reference(
        str(draft.get("shopify_order_number", "")),
        parcel.package_type,
        1,
        1,
    )

    return {
        "reference_number": reference_number,
        # Optional since 2026-07-01 — Allegro auto-derives it from the order.
        "delivery_method_id": draft.get("allegro_delivery_method_id") or None,
        "credentials_id": draft.get("allegro_credentials_id"),
        "packages": packages,
        "sender": sender,
        "receiver": receiver,
        "additional_properties": additional_properties,
    }


def allegro_payload_plan(
    draft: dict[str, Any],
    client: AllegroDeliveryProposalClient,
) -> list[dict[str, Any]]:
    """Return the exact Allegro create-command payload after one proposal GET."""
    order_id = str(draft.get("external_order_id") or "")
    if not order_id:
        raise RuntimeError("Ship with Allegro requires external_order_id")
    proposal = client.get_delivery_proposal(order_id)
    call_spec = allegro_call_spec(draft, proposal)
    return [
        {
            "service": draft.get("service"),
            "package_type": "allegro",
            "package_number": 1,
            "reference": call_spec["reference_number"],
            "payload": call_spec,
        }
    ]


__all__ = [
    "ALLEGRO_INPOST_SENDING_METHODS",
    "AllegroCallSpec",
    "AllegroDeliveryProposalClient",
    "allegro_call_spec",
    "allegro_payload_plan",
]
