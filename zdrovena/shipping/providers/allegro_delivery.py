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


class AllegroDeliveryProposalClient(Protocol):
    """Read-only Allegro capability required to plan a shipment preview."""

    def get_delivery_proposal(self, order_id: str) -> dict[str, Any]: ...


def allegro_call_specs(draft: dict[str, Any], proposal: dict[str, Any]) -> list[AllegroCallSpec]:
    """Build one independent create-command for each physical parcel."""
    parcels = physical_parcels(draft)

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

    # Allegro stopped supporting ``inpost#sendingMethod`` on 1 March 2026.
    # Point sending is now the ``sendingAtPoint`` service in the order-specific
    # proposal, so only forward it when Allegro proposes it. Never infer it
    # from the obsolete draft enum.
    proposed_services = suggested_input.get("additionalServices") or []
    additional_services = ["sendingAtPoint"] if "sendingAtPoint" in proposed_services else None

    first_parcel = parcels[0]
    reference_number = shipment_reference(
        str(draft.get("shopify_order_number", "")),
        first_parcel.package_type,
        1,
        1,
    )

    call_specs: list[AllegroCallSpec] = []
    for parcel in parcels:
        parcel_spec = PARCEL_SPECS.get(parcel.package_type) or PARCEL_SPECS["1-pak"]
        package = {
            "type": "PACKAGE",
            "length": {"value": parcel_spec["length"], "unit": "CENTIMETER"},
            "width": {"value": parcel_spec["width"], "unit": "CENTIMETER"},
            "height": {"value": parcel_spec["height"], "unit": "CENTIMETER"},
            "weight": {
                "value": round(float(parcel_spec["weight_kg"]), 2),
                "unit": "KILOGRAMS",
            },
        }
        call_specs.append(
            {
                "package_type": parcel.package_type,
                "package_number": parcel.position,
                "kwargs": {
                    "reference_number": reference_number,
                    # Optional since 2026-07-01 — Allegro derives it from the order.
                    "delivery_method_id": draft.get("allegro_delivery_method_id") or None,
                    "credentials_id": draft.get("allegro_credentials_id"),
                    "packages": [package],
                    "sender": sender,
                    "receiver": dict(receiver),
                    "additional_services": additional_services,
                    "additional_properties": None,
                },
            }
        )
    return call_specs


def allegro_call_spec(draft: dict[str, Any], proposal: dict[str, Any]) -> AllegroCallSpec:
    """Return the first physical parcel's create-command arguments."""
    return allegro_call_specs(draft, proposal)[0]["kwargs"]


def allegro_payload_plan(
    draft: dict[str, Any],
    client: AllegroDeliveryProposalClient,
) -> list[dict[str, Any]]:
    """Return the exact Allegro create-command payload after one proposal GET."""
    order_id = str(draft.get("external_order_id") or "")
    if not order_id:
        raise RuntimeError("Ship with Allegro requires external_order_id")
    proposal = client.get_delivery_proposal(order_id)
    call_specs = allegro_call_specs(draft, proposal)
    return [
        {
            "service": draft.get("service"),
            "package_type": call_spec["package_type"],
            "package_number": call_spec["package_number"],
            "reference": call_spec["kwargs"]["reference_number"],
            "payload": call_spec["kwargs"],
        }
        for call_spec in call_specs
    ]


__all__ = [
    "AllegroCallSpec",
    "AllegroDeliveryProposalClient",
    "allegro_call_spec",
    "allegro_call_specs",
    "allegro_payload_plan",
]
