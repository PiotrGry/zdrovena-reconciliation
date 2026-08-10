"""HTTP-neutral Allegro Delivery shipment planning."""

from __future__ import annotations

from typing import Any, Protocol

from zdrovena.common.shipping_exceptions import AllegroBusinessError
from zdrovena.shipping.domain.planning import parcel_weight_and_dims

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
    weight_kg, dims = parcel_weight_and_dims(draft)
    packages = [
        {
            "type": "PACKAGE",
            "length": {"value": dims["length"], "unit": "CENTIMETER"},
            "width": {"value": dims["width"], "unit": "CENTIMETER"},
            "height": {"value": dims["height"], "unit": "CENTIMETER"},
            "weight": {"value": round(weight_kg, 2), "unit": "KILOGRAMS"},
        }
    ]

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

    return {
        "order_id": str(draft.get("external_order_id") or ""),
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
    return [
        {
            "service": draft.get("service"),
            "package_type": "allegro",
            "package_number": 1,
            "reference": order_id,
            "payload": allegro_call_spec(draft, proposal),
        }
    ]


__all__ = [
    "ALLEGRO_INPOST_SENDING_METHODS",
    "AllegroCallSpec",
    "AllegroDeliveryProposalClient",
    "allegro_call_spec",
    "allegro_payload_plan",
]
