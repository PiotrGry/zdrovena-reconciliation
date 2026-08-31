"""HTTP-neutral InPost parcel planning and shipment resume behavior."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from zdrovena.common.shipping_exceptions import (
    InPostBusinessError,
    InPostRecipientPhoneError,
)
from zdrovena.common.shipping_format import normalize_pl_phone
from zdrovena.common.shipping_parcels import PARCEL_SPECS
from zdrovena.shipping.domain.cod import CodAllocationError, cod_allocation
from zdrovena.shipping.domain.planning import physical_parcels, shipment_reference

InPostCallSpec = tuple[str, str, int, str, dict[str, Any]]
InPostCallSpecsBuilder = Callable[[dict[str, Any], dict[str, str]], list[InPostCallSpec]]
ShipmentPatchBuilder = Callable[[list[dict[str, str]]], dict[str, Any]]


class InPostPayloadBuilder(Protocol):
    """Payload-builder capabilities used by preview planning."""

    def build_paczkomat_payload(
        self,
        *,
        receiver_first_name: str,
        receiver_last_name: str,
        receiver_email: str,
        receiver_phone: str,
        target_point: str,
        reference: str,
        template: str = "small",
        cod_amount: str | None = None,
        cod_currency: str = "PLN",
    ) -> dict[str, Any]: ...

    def build_kurier_payload(
        self,
        *,
        receiver_first_name: str,
        receiver_last_name: str,
        receiver_email: str,
        receiver_phone: str,
        receiver_street: str,
        receiver_building_number: str,
        receiver_city: str,
        receiver_post_code: str,
        sender: dict[str, str],
        reference: str,
        weight_kg: float = 1.0,
        dimensions: dict[str, float] | None = None,
        cod_amount: str | None = None,
        cod_currency: str = "PLN",
    ) -> dict[str, Any]: ...


def inpost_call_specs(draft: dict[str, Any], sender: dict[str, str]) -> list[InPostCallSpec]:
    """Expand a draft into the exact builder arguments for each physical parcel."""
    if draft.get("cod_error"):
        raise InPostBusinessError(
            f"Invalid Shopify COD data: {draft['cod_error']}",
            courier="inpost",
            action="create_shipment",
        )
    receiver = draft.get("receiver") or {}
    # InPost enforces a valid recipient phone from 2026-09-08 (issue #294).
    # Validated here rather than at the call site: this is the one funnel both
    # the paczkomat and the kurier path share, it is pure, and it runs before
    # the paid ShipX POST. The operator's execution preview goes through the
    # same function, so the reason surfaces before they press send.
    raw_phone = receiver.get("phone")
    receiver_phone = normalize_pl_phone(raw_phone)
    if not receiver_phone:
        raise InPostRecipientPhoneError(
            raw_phone=str(raw_phone or ""),
            order_id=str(draft.get("shopify_order_number") or ""),
        )
    addr = draft.get("shipping_address") or {}
    order_number = str(draft.get("shopify_order_number", ""))
    inpost_service = "paczkomat" if draft.get("service") == "inpost_locker_standard" else "kurier"

    parcels = physical_parcels(draft)
    cod = draft.get("cod")
    cod_amounts: list[str] = []
    if cod:
        # A locker is collected parcel by parcel, so splitting the amount there
        # would let the customer pay for one box and leave the rest behind. A
        # courier hands over everything in one visit, so the split is safe.
        if inpost_service == "paczkomat" and len(parcels) != 1:
            raise InPostBusinessError(
                "COD to a Paczkomat locker requires exactly one physical shipment; "
                "repack the order into one parcel or send it by courier",
                courier="inpost",
                action="create_shipment",
            )
        try:
            cod_amounts = [str(amount) for amount in cod_allocation(draft).amounts]
        except CodAllocationError as exc:
            raise InPostBusinessError(
                str(exc), courier="inpost", action="create_shipment"
            ) from exc

    specs: list[InPostCallSpec] = []
    for position, parcel in enumerate(parcels):
        package_type = parcel.package_type
        package_number = parcel.position
        package_count = parcel.count_for_type
        spec = PARCEL_SPECS.get(package_type, PARCEL_SPECS["1-pak"])
        reference = shipment_reference(order_number, package_type, package_number, package_count)
        if inpost_service == "paczkomat":
            kwargs: dict[str, Any] = {
                "receiver_first_name": receiver.get("first_name", ""),
                "receiver_last_name": receiver.get("last_name", ""),
                "receiver_email": receiver.get("email", ""),
                "receiver_phone": receiver_phone,
                "target_point": receiver.get("locker_id", ""),
                "reference": reference,
                "template": spec.get("paczkomat_template") or "large",
            }
        else:
            kwargs = {
                "receiver_first_name": receiver.get("first_name", ""),
                "receiver_last_name": receiver.get("last_name", ""),
                "receiver_email": receiver.get("email", ""),
                "receiver_phone": receiver_phone,
                "receiver_street": addr.get("street", ""),
                "receiver_building_number": "/".join(
                    filter(None, [addr.get("building_number", "1"), addr.get("flat_number", "")])
                ),
                "receiver_city": addr.get("city", ""),
                "receiver_post_code": addr.get("post_code", ""),
                "sender": sender,
                "reference": reference,
                "weight_kg": spec["weight_kg"],
                "dimensions": spec,
            }
        if cod:
            kwargs.update(
                {
                    "cod_amount": cod_amounts[position],
                    "cod_currency": str(cod.get("currency") or ""),
                }
            )
        specs.append((inpost_service, package_type, package_number, reference, kwargs))
    return specs


def pending_inpost_call_specs(
    draft: dict[str, Any],
    sender: dict[str, str],
    *,
    build_call_specs: InPostCallSpecsBuilder | None = None,
) -> list[InPostCallSpec]:
    """Return only parcel calls not already present in courier checkpoints."""
    existing_keys = {
        (str(item.get("package_type")), int(item.get("package_number") or 1))
        for item in draft.get("courier_shipments") or []
    }
    call_specs = (build_call_specs or inpost_call_specs)(draft, sender)
    return [spec for spec in call_specs if (spec[1], spec[2]) not in existing_keys]


def inpost_payload_plan(
    draft: dict[str, Any],
    sender: dict[str, str],
    builder: InPostPayloadBuilder,
    *,
    build_pending_call_specs: InPostCallSpecsBuilder | None = None,
) -> list[dict[str, Any]]:
    """Return the exact ShipX payloads this draft would produce, without sending."""
    plan: list[dict[str, Any]] = []
    pending_call_specs = (build_pending_call_specs or pending_inpost_call_specs)(draft, sender)
    for (
        inpost_service,
        package_type,
        package_number,
        reference,
        kwargs,
    ) in pending_call_specs:
        if inpost_service == "paczkomat":
            payload = builder.build_paczkomat_payload(**kwargs)
        else:
            payload = builder.build_kurier_payload(**kwargs)
        plan.append(
            {
                "service": draft.get("service"),
                "package_type": package_type,
                "package_number": package_number,
                "reference": reference,
                "payload": payload,
            }
        )
    return plan


def is_resumable_inpost_draft(draft: dict[str, Any]) -> bool:
    """Return whether a ShipX shipment is waiting for confirmation."""
    return bool(
        draft.get("status") == "pending_confirmation"
        and str(draft.get("courier_draft_id") or "").strip()
    )


def resume_inpost_shipment(
    client: Any,
    draft: dict[str, Any],
    *,
    build_patch: ShipmentPatchBuilder,
) -> dict[str, Any]:
    """Refresh every existing ShipX shipment without creating another one."""
    existing = list(draft.get("courier_shipments") or [])
    refreshed_shipments: list[dict[str, str]] = []

    if existing:
        for shipment in existing:
            refreshed = dict(shipment)
            shipment_id = str(shipment.get("id") or "").strip()
            tracking_number = str(shipment.get("tracking_number") or "").strip()
            if shipment_id and not tracking_number:
                result = client.get_shipment(shipment_id)
                if not result.get("tracking_number"):
                    result = client.wait_for_shipment_confirmation(
                        shipment_id,
                        max_attempts=3,
                        interval_s=1.0,
                    )
                refreshed["tracking_number"] = str(result.get("tracking_number") or "")
            refreshed_shipments.append(refreshed)
    else:
        # Historical drafts predate courier_shipments and only carry the first
        # ShipX id in courier_draft_id. Preserve their legacy patch shape.
        shipment_id = str(draft.get("courier_draft_id") or "").strip()
        result = client.get_shipment(shipment_id)
        if not result.get("tracking_number"):
            result = client.wait_for_shipment_confirmation(
                shipment_id,
                max_attempts=3,
                interval_s=1.0,
            )
        refreshed_shipments.append(
            {
                "id": str(result.get("id") or shipment_id),
                "tracking_number": str(result.get("tracking_number") or ""),
                "package_type": "1-pak",
                "package_number": "1",
            }
        )

    patch = build_patch(refreshed_shipments)
    for field in ("dispatch_order_id", "pickup_ordered"):
        if field in draft:
            patch[field] = draft[field]
    return patch


__all__ = [
    "InPostCallSpec",
    "InPostCallSpecsBuilder",
    "InPostPayloadBuilder",
    "ShipmentPatchBuilder",
    "inpost_call_specs",
    "inpost_payload_plan",
    "is_resumable_inpost_draft",
    "pending_inpost_call_specs",
    "resume_inpost_shipment",
]
