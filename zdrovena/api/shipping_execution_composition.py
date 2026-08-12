"""Concrete API composition for shipping execution and confirmation.

Provider clients and secret-backed runtime integration stay concrete here. The
``shipping.application`` workflow remains HTTP- and provider-client-neutral.
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, NoReturn

from fastapi import HTTPException, status

from zdrovena.api.shipping_draft_composition import emit_tracking_assigned
from zdrovena.common.events import log_event
from zdrovena.common.exceptions import MissingSecretError
from zdrovena.common.secrets import get_secret
from zdrovena.common.shipping_exceptions import (
    AllegroAuthError,
    AllegroBusinessError,
    AllegroCommandPending,
    ApaczkaBusinessError,
    CourierTransientError,
    InPostBusinessError,
)
from zdrovena.common.shipping_store import DLQ_KIND_EXECUTION
from zdrovena.shipping.application.execution import fingerprint as execution_fingerprint
from zdrovena.shipping.application.execution import workflow as execution_workflow
from zdrovena.shipping.domain.planning import parcel_weight_and_dims
from zdrovena.shipping.providers import allegro_delivery as allegro_delivery_provider
from zdrovena.shipping.providers import apaczka as apaczka_provider
from zdrovena.shipping.providers import inpost as inpost_provider

SHIPMENT_ORIGIN_SYSTEM = "system"
SHIPMENT_ORIGIN_EXTERNAL = "external"
MOCK_COURIER = os.getenv("MOCK_COURIER", "").lower() in ("1", "true", "yes")
_APACZKA_SERVICES_REQUIRING_PICKUP_POINT = frozenset({"23", "64"})

logger = logging.getLogger("zdrovena.api.shipping_execution_composition")


def get_sender() -> dict[str, str]:
    name = get_secret("sender_name", required=False) or ""
    return {
        "name": name,
        "firstname": "",
        "lastname": name,
        "street": get_secret("sender_street", required=False) or "",
        "building_number": get_secret("sender_building_number", required=False) or "1",
        "city": get_secret("sender_city", required=False) or "",
        "post_code": get_secret("sender_post_code", required=False) or "",
        "phone": get_secret("sender_phone", required=False) or "",
        "email": get_secret("sender_email", required=False) or "",
    }


def get_pickup_address() -> dict[str, str]:
    """Return the physical courier collection address."""
    name = get_secret("pickup_name")
    return {
        "name": name,
        "firstname": "",
        "lastname": name,
        "street": get_secret("pickup_street"),
        "building_number": get_secret("pickup_building_number"),
        "city": get_secret("pickup_city"),
        "post_code": get_secret("pickup_post_code"),
        "phone": get_secret("pickup_phone", required=False) or get_secret("sender_phone"),
        "email": get_secret("pickup_email"),
    }


def allegro_carrier_id_for_courier(courier: str) -> str:
    return "INPOST" if courier == "inpost" else "OTHER"


def get_allegro_client() -> Any | None:
    """Build an Allegro client with durable refresh-token rotation."""
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


def push_tracking_to_allegro(draft: dict[str, Any]) -> None:
    """Best-effort tracking push for non-native Allegro shipping drafts."""
    if draft.get("source") != "allegro" or draft.get("courier") == "allegro_delivery":
        return
    tracking = draft.get("tracking_number")
    external_id = str(draft.get("external_order_id") or "")
    if not tracking or not external_id:
        return
    client = get_allegro_client()
    if client is None:
        logger.warning(
            "Allegro credentials missing — cannot push tracking %s for order %s",
            tracking,
            external_id,
        )
        return
    carrier_id = allegro_carrier_id_for_courier(draft.get("courier", ""))
    try:
        client.create_shipment(
            order_id=external_id,
            carrier_id=carrier_id,
            waybill=tracking,
        )
        logger.info(
            "Pushed tracking %s to Allegro order %s (%s)", tracking, external_id, carrier_id
        )
    except Exception:
        logger.exception("Failed to push tracking to Allegro for order %s", external_id)


def _parcel_template(draft: dict[str, Any]) -> str:
    from zdrovena.common.inpost import PARCEL_SPECS, pick_paczkomat_template

    breakdown = draft.get("packages_breakdown") or []
    total_weight, largest_dims = parcel_weight_and_dims(draft)
    if breakdown and largest_dims:
        auto = pick_paczkomat_template(dict(largest_dims), total_weight)
        if auto:
            return auto
    for box_type in ("3-pak", "szkło-2pak", "2-pak", "szkło", "1-pak", "pół-pak"):
        if any(box.get("type") == box_type for box in breakdown):
            template = PARCEL_SPECS.get(box_type, {}).get("paczkomat_template")
            return template if template else "large"
    return "large"


def shipment_patch(shipments: list[dict[str, str]]) -> dict[str, Any]:
    """Project all physical provider shipments onto the draft lifecycle."""
    first = shipments[0] if shipments else {}
    confirmed = bool(shipments) and all(
        str(shipment.get("tracking_number") or "").strip() for shipment in shipments
    )
    return {
        "courier_draft_id": first.get("id"),
        "courier_shipments": shipments,
        "dispatch_order_id": None,
        "tracking_number": first.get("tracking_number"),
        "status": "created" if confirmed else "pending_confirmation",
        "pickup_ordered": False,
        "error": None,
    }


def dispatch_shipment_ids(record: dict[str, Any]) -> list[str]:
    """Return every physical shipment ID in stable order."""
    shipment_ids = [
        str(shipment.get("id") or "").strip() for shipment in record.get("courier_shipments") or []
    ]
    shipment_ids = [shipment_id for shipment_id in shipment_ids if shipment_id]
    if shipment_ids:
        return shipment_ids
    legacy = str(record.get("courier_draft_id") or "").strip()
    return [legacy] if legacy else []


def order_inpost_pickup(
    draft: dict[str, Any],
    pickup_date: str | None,
    pickup_from: str | None,
    pickup_to: str | None,
) -> dict[str, Any]:
    """Order one InPost dispatch for every physical shipment in a draft."""
    from zdrovena.common.inpost import InPostClient

    client = InPostClient(
        get_secret("inpost_api_token"),
        get_secret("inpost_organization_id"),
    )
    return client.create_dispatch_order(
        dispatch_shipment_ids(draft),
        get_pickup_address(),
        pickup_date=pickup_date,
        pickup_from=pickup_from,
        pickup_to=pickup_to,
    )


def _run_inpost(
    draft: dict[str, Any],
    sender: dict[str, str],
    *,
    pickup_date: str | None = None,
    pickup_from: str | None = None,
    pickup_to: str | None = None,
    on_shipment_created: Callable[[dict[str, str]], None] | None = None,
) -> dict[str, Any]:
    """Create or resume InPost parcels without duplicate provider writes."""
    if MOCK_COURIER:
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

    client = InPostClient(get_secret("inpost_api_token"), get_secret("inpost_organization_id"))
    existing = list(draft.get("courier_shipments") or [])
    if inpost_provider.is_resumable_inpost_draft(draft):
        return inpost_provider.resume_inpost_shipment(client, draft, build_patch=shipment_patch)

    for (
        service,
        package_type,
        package_number,
        _reference,
        kwargs,
    ) in inpost_provider.pending_inpost_call_specs(draft, sender):
        result = (
            client.create_paczkomat_shipment(**kwargs)
            if service == "paczkomat"
            else client.create_kurier_shipment(**kwargs)
        )
        shipment = {
            "id": str(result.get("id", "")),
            "tracking_number": str(result.get("tracking_number") or ""),
            "package_type": package_type,
            "package_number": str(package_number),
        }
        existing.append(shipment)
        if on_shipment_created:
            on_shipment_created(shipment)

    patch = shipment_patch(existing)
    if pickup_date and patch.get("status") == "created":
        shipment_ids = dispatch_shipment_ids(patch)
        try:
            dispatch = client.create_dispatch_order(
                shipment_ids,
                get_pickup_address(),
                pickup_date=pickup_date,
                pickup_from=pickup_from,
                pickup_to=pickup_to,
            )
            patch["dispatch_order_id"] = str(dispatch.get("id") or "") or None
            patch["pickup_ordered"] = True
        except Exception:
            logger.exception("InPost dispatch order failed for shipments %s", shipment_ids)
    return patch


def _run_apaczka(
    draft: dict[str, Any],
    storage: Any,
    *,
    pickup_address: dict[str, str] | None = None,
    pickup_date: str | None = None,
    pickup_from: str | None = None,
    pickup_to: str | None = None,
    on_shipment_created: Callable[[dict[str, str]], None] | None = None,
) -> dict[str, Any]:
    if MOCK_COURIER:
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

    service_id = draft.get("apaczka_service_id") or ""
    if not service_id:
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
    apaczka_provider.validate_apaczka_pickup_window(
        str(service_id),
        pickup_date,
        pickup_from,
        pickup_to,
    )
    client = ApaczkaClient(
        get_secret("apaczka_app_id"),
        get_secret("apaczka_app_secret"),
        service_id,
        storage,
    )
    existing = list(draft.get("courier_shipments") or [])
    for call_spec in apaczka_provider.apaczka_call_specs(
        draft,
        pickup_address or get_pickup_address(),
        pickup_date=pickup_date,
        pickup_from=pickup_from,
        pickup_to=pickup_to,
    ):
        result = client.create_shipment(**call_spec["kwargs"])
        shipment = {
            "id": str(result.get("id", "")),
            "tracking_number": str(result.get("waybill_number") or ""),
            "package_type": call_spec["package_type"],
            "package_number": str(call_spec["package_number"]),
        }
        existing.append(shipment)
        if on_shipment_created:
            on_shipment_created(shipment)
    return shipment_patch(existing)


class AllegroPickupTerminalError(AllegroBusinessError):
    """A pickup command reached a provider terminal state and cannot resume."""


def get_allegro_pickup_address() -> dict[str, str]:
    pickup = get_pickup_address()
    street = " ".join(
        part
        for part in (
            str(pickup.get("street") or "").strip(),
            str(pickup.get("building_number") or "").strip(),
        )
        if part
    )
    return {
        "name": str(pickup.get("name") or ""),
        "street": street,
        "postalCode": str(pickup.get("post_code") or ""),
        "city": str(pickup.get("city") or ""),
        "countryCode": "PL",
        "email": str(pickup.get("email") or ""),
        "phone": str(pickup.get("phone") or ""),
    }


def order_allegro_pickup(
    client: Any,
    shipment_ids: list[str],
    pickup_date: str | None,
    *,
    command_id: str | None = None,
    on_command_created: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Create or resume one asynchronous Ship-with-Allegro pickup command."""
    if not shipment_ids:
        raise AllegroBusinessError(
            detail="pickup requires at least one shipmentId",
            action="create_pickup_command",
        )
    if command_id is None:
        pickup_address = get_allegro_pickup_address()
        proposals = client.get_ship_with_allegro_pickup_proposals(
            shipment_ids, address=pickup_address
        )
        new_format = next((proposal for proposal in proposals if proposal.get("date")), None)
        legacy_format = next(
            (proposal for proposal in proposals if proposal.get("id") and not proposal.get("date")),
            None,
        )
        selected = new_format or legacy_format
        if not selected:
            logger.warning(
                "No pickup proposals available for shipment %s on %s",
                ",".join(shipment_ids),
                pickup_date,
            )
            return {
                "status": "NO_SLOT",
                "command_id": None,
                "pickup_id": None,
                "carrier_pickup_id": None,
            }
        command_id = str(uuid.uuid4())
        if selected.get("date"):
            client.create_ship_with_allegro_pickup(
                command_id=command_id,
                shipment_ids=shipment_ids,
                address=pickup_address,
                pickup_time={
                    "date": selected["date"],
                    "minTime": selected.get("minTime", "08:00"),
                    "maxTime": selected.get("maxTime", "18:00"),
                },
            )
        else:
            client.create_ship_with_allegro_pickup(
                command_id=command_id,
                shipment_ids=shipment_ids,
                address=pickup_address,
                proposal_item_id=selected["id"],
            )
        if on_command_created:
            on_command_created(command_id)

    status_payload = client.get_ship_with_allegro_pickup_command_status(command_id)
    command_status = str((status_payload or {}).get("status") or "")
    if command_status == "IN_PROGRESS":
        return {
            "status": command_status,
            "command_id": command_id,
            "pickup_id": None,
            "carrier_pickup_id": None,
        }
    if command_status == "ERROR":
        errors = (status_payload or {}).get("errors") or []
        detail = "; ".join(str(error.get("message") or error) for error in errors)
        raise AllegroPickupTerminalError(
            detail=detail or f"pickup command {command_id} failed",
            action="get_pickup_command_status",
        )
    if command_status != "SUCCESS":
        raise AllegroBusinessError(
            detail=f"unexpected pickup command status: {command_status!r}",
            action="get_pickup_command_status",
        )
    pickup_id = str((status_payload or {}).get("pickupId") or "")
    if not pickup_id:
        raise AllegroPickupTerminalError(
            detail=f"pickup command {command_id} succeeded without pickupId",
            action="get_pickup_command_status",
        )
    return {
        "status": command_status,
        "command_id": command_id,
        "pickup_id": pickup_id,
        "carrier_pickup_id": (status_payload or {}).get("carrierPickupId"),
    }


def _run_allegro_delivery(
    draft: dict[str, Any],
    *,
    pickup_date: str | None = None,
    pickup_from: str | None = None,
    pickup_to: str | None = None,
    on_command_created: Callable[[str], None] | None = None,
    on_pickup_command_created: Callable[[str], None] | None = None,
    on_shipment_checkpoint: Callable[[dict[str, str]], None] | None = None,
) -> dict[str, Any]:
    del pickup_from, pickup_to
    if MOCK_COURIER:
        ref = draft.get("shopify_order_number", "mock")
        logger.info("MOCK_COURIER: skipping Allegro Delivery API for order %s", ref)
        return {
            "courier_draft_id": f"mock-allegro-{ref}",
            "tracking_number": f"AWA{ref}00000",
            "status": "created",
            "pickup_ordered": False,
            "error": None,
        }

    client = get_allegro_client()
    if client is None:
        raise RuntimeError("Allegro credentials missing — cannot use Ship with Allegro")
    order_id = str(draft.get("external_order_id") or "")
    if not order_id:
        raise RuntimeError("Ship with Allegro requires external_order_id")
    proposal = client.get_delivery_proposal(order_id)
    call_specs = allegro_delivery_provider.allegro_call_specs(draft, proposal)
    existing_shipments = [dict(item) for item in draft.get("courier_shipments") or []]
    shipments_by_key = {
        (str(item.get("package_type") or ""), int(item.get("package_number") or 1)): item
        for item in existing_shipments
    }
    legacy_command_id = (
        str(draft.get("allegro_command_id") or "")
        if draft.get("status") == "pending_confirmation"
        else ""
    )
    active_command_id: str | None = None

    def ordered_shipments() -> list[dict[str, str]]:
        return [
            shipments_by_key[(str(spec["package_type"]), int(spec["package_number"]))]
            for spec in call_specs
            if (str(spec["package_type"]), int(spec["package_number"])) in shipments_by_key
        ]

    def shipment_result(status_value: str) -> dict[str, Any]:
        shipments = ordered_shipments()
        first = shipments[0] if shipments else {}
        return {
            "courier_draft_id": first.get("id") or None,
            "allegro_shipment_id": first.get("id") or None,
            "courier_shipments": shipments,
            "tracking_number": first.get("tracking_number") or None,
            "status": status_value,
            "pickup_ordered": False,
            "allegro_command_id": active_command_id,
            "error": None,
        }

    for index, call_spec in enumerate(call_specs):
        key = (str(call_spec["package_type"]), int(call_spec["package_number"]))
        shipment_checkpoint = shipments_by_key.get(key)
        if shipment_checkpoint and shipment_checkpoint.get("id"):
            continue

        command_id = str((shipment_checkpoint or {}).get("allegro_command_id") or "")
        if not command_id and index == 0:
            command_id = legacy_command_id
        if not command_id:
            command_id = str(uuid.uuid4())
            client.create_ship_with_allegro_shipment(
                command_id=command_id,
                **call_spec["kwargs"],
            )
            if on_command_created:
                on_command_created(command_id)

        active_command_id = command_id
        shipment_checkpoint = {
            **(shipment_checkpoint or {}),
            "id": "",
            "tracking_number": "",
            "package_type": key[0],
            "package_number": str(key[1]),
            "allegro_command_id": command_id,
        }
        shipments_by_key[key] = shipment_checkpoint
        if on_shipment_checkpoint:
            on_shipment_checkpoint(dict(shipment_checkpoint))

        try:
            shipment_id = client.wait_for_ship_with_allegro_shipment(
                command_id, max_attempts=3, interval_s=1.0
            )
        except AllegroCommandPending as exc:
            active_command_id = exc.command_id or command_id
            logger.info(
                "Allegro Delivery create-command %s pending — draft %s",
                active_command_id,
                draft.get("id"),
            )
            return shipment_result("pending_confirmation")

        shipment = client.get_ship_with_allegro_shipment(shipment_id)
        _carrier_id, waybill = client.extract_shipment_waybill(shipment)
        shipment_checkpoint.update(
            {
                "id": str(shipment_id),
                "tracking_number": str(waybill or ""),
            }
        )
        if on_shipment_checkpoint:
            on_shipment_checkpoint(dict(shipment_checkpoint))

    created_shipments = ordered_shipments()
    shipment_ids = [str(shipment["id"]) for shipment in created_shipments if shipment.get("id")]
    pickup_ordered = False
    allegro_dispatch_id: str | None = None
    allegro_pickup_command_id: str | None = None
    if pickup_date:
        pickup_command_id = (
            str(draft.get("allegro_pickup_command_id") or "")
            if not draft.get("pickup_ordered")
            else ""
        ) or None

        def checkpoint_pickup(command_id: str) -> None:
            nonlocal pickup_command_id
            pickup_command_id = command_id
            if on_pickup_command_created:
                on_pickup_command_created(command_id)

        try:
            pickup_result = order_allegro_pickup(
                client,
                shipment_ids,
                pickup_date,
                command_id=pickup_command_id,
                on_command_created=checkpoint_pickup,
            )
            pickup_ordered = pickup_result["status"] == "SUCCESS"
            if pickup_ordered:
                allegro_dispatch_id = pickup_result["pickup_id"]
            else:
                allegro_pickup_command_id = pickup_result["command_id"]
        except (
            AllegroBusinessError,
            AllegroAuthError,
            CourierTransientError,
            MissingSecretError,
        ) as exc:
            logger.exception("Allegro Delivery pickup failed for %s", ",".join(shipment_ids))
            if not isinstance(exc, AllegroPickupTerminalError):
                allegro_pickup_command_id = pickup_command_id

    result = shipment_result("created")
    result.update(
        {
            "pickup_ordered": pickup_ordered,
            "allegro_dispatch_id": allegro_dispatch_id,
            "allegro_pickup_command_id": allegro_pickup_command_id,
        }
    )
    return result


def execution_preview(
    draft: dict[str, Any],
    *,
    pickup_date: str | None = None,
    pickup_from: str | None = None,
    pickup_to: str | None = None,
) -> dict[str, Any]:
    """Build the exact provider preview and reviewed-snapshot fingerprint."""
    courier = draft.get("courier", "apaczka")
    if courier == "inpost":
        from zdrovena.common.inpost import InPostClient

        sender = get_sender()
        preview: dict[str, Any] = {
            "courier": courier,
            "sender": sender,
            "parcels": inpost_provider.inpost_payload_plan(
                draft, sender, InPostClient("preview", "preview")
            ),
            "preview_available": True,
        }
    elif courier == "apaczka":
        from zdrovena.common.apaczka import ApaczkaClient

        pickup_address = get_pickup_address()
        preview = {
            "courier": courier,
            "sender": pickup_address,
            "parcels": apaczka_provider.apaczka_payload_plan(
                draft,
                pickup_address,
                ApaczkaClient(
                    "preview",
                    "preview",
                    str(draft.get("apaczka_service_id") or ""),
                    None,
                ),
                pickup_date=pickup_date,
                pickup_from=pickup_from,
                pickup_to=pickup_to,
            ),
            "preview_available": True,
        }
    elif courier == "allegro_delivery":
        try:
            order_id = str(draft.get("external_order_id") or "")
            if not order_id:
                raise RuntimeError("Ship with Allegro requires external_order_id")
            allegro = get_allegro_client()
            if allegro is None:
                raise RuntimeError("Allegro client is not configured")
            parcels = allegro_delivery_provider.allegro_payload_plan(draft, allegro)
        except Exception as exc:
            logger.warning("Allegro preview unavailable for draft %s: %s", draft.get("id"), exc)
            preview = {
                "courier": courier,
                "sender": {},
                "parcels": [],
                "preview_available": False,
                "note": (
                    "Nie udało się pobrać propozycji dostawy z Allegro, więc nie "
                    "wiadomo, co dokładnie zostałoby wysłane. Wysyłka i tak by się "
                    "nie powiodła — spróbuj ponownie za chwilę."
                ),
            }
        else:
            preview = {
                "courier": courier,
                "sender": (parcels[0]["payload"].get("sender") if parcels else {}) or {},
                "parcels": parcels,
                "preview_available": True,
            }
    else:
        preview = {
            "courier": courier,
            "sender": get_sender(),
            "parcels": [],
            "preview_available": False,
            "note": f"Podgląd nie jest jeszcze dostępny dla kuriera {courier}.",
        }
    return {
        **preview,
        "fingerprint": execution_fingerprint.preview_fingerprint(draft, preview),
    }


EXECUTION_APPLICATION_HTTP_ERRORS = (
    execution_workflow.DraftNotFoundError,
    execution_workflow.DraftRequiresReviewError,
    execution_workflow.PreviewFingerprintMismatchError,
    execution_workflow.ExecutionClaimConflictError,
    execution_workflow.ExecutionCommunicationError,
)


def raise_execution_http_exception(exc: Exception) -> NoReturn:
    """Translate known execution application failures at the FastAPI edge."""
    if isinstance(exc, execution_workflow.DraftNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(
        exc,
        (
            execution_workflow.DraftRequiresReviewError,
            execution_workflow.PreviewFingerprintMismatchError,
            execution_workflow.ExecutionClaimConflictError,
        ),
    ):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, execution_workflow.ExecutionCommunicationError):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Błąd komunikacji z przewoźnikiem — spróbuj ponownie za chwilę.",
        ) from exc.original
    raise exc


def _provider_runners(storage: Any) -> execution_workflow.ProviderRunners:
    def run_apaczka(draft: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        return _run_apaczka(draft, storage, **kwargs)

    return execution_workflow.ProviderRunners(
        inpost=_run_inpost,
        apaczka=run_apaczka,
        allegro_delivery=_run_allegro_delivery,
    )


def _execution_effects() -> execution_workflow.ExecutionEffects:
    return execution_workflow.ExecutionEffects(
        record_event=log_event,
        emit_tracking_assigned=emit_tracking_assigned,
        push_tracking=push_tracking_to_allegro,
        log_exception=logger.exception,
    )


def execute_shipping_draft(
    draft_id: str,
    repository: execution_workflow.ExecutionRepository,
    storage: Any,
    *,
    pickup_window: execution_workflow.PickupWindow | None = None,
    preview_fingerprint: str | None = None,
    failure_dlq_entry_id: str | None = None,
) -> dict[str, Any]:
    """Execute one draft using the concrete API runtime composition."""
    draft = repository.get_draft(draft_id)
    effective_pickup_window = pickup_window or execution_workflow.PickupWindow()
    if draft and draft.get("courier") == "apaczka":
        apaczka_provider.validate_apaczka_pickup_window(
            str(draft.get("apaczka_service_id") or ""),
            effective_pickup_window.date,
            effective_pickup_window.from_time,
            effective_pickup_window.to_time,
        )

    def build_preview(current_draft: dict[str, Any]) -> dict[str, Any]:
        return execution_preview(
            current_draft,
            pickup_date=effective_pickup_window.date,
            pickup_from=effective_pickup_window.from_time,
            pickup_to=effective_pickup_window.to_time,
        )

    return execution_workflow.execute_draft(
        draft_id,
        repository,
        build_preview=build_preview,
        resolve_sender=get_sender,
        providers=_provider_runners(storage),
        effects=_execution_effects(),
        execution_dlq_kind=DLQ_KIND_EXECUTION,
        system_shipment_origin=SHIPMENT_ORIGIN_SYSTEM,
        pickup_window=effective_pickup_window,
        preview_fingerprint=preview_fingerprint,
        failure_dlq_entry_id=failure_dlq_entry_id,
    )


@dataclass(frozen=True)
class ConfirmationResult:
    """HTTP-neutral confirmation payload plus its response status."""

    payload: dict[str, Any]
    status_code: int = 200


class ConfirmationError(RuntimeError):
    """Known confirmation failure awaiting FastAPI translation."""

    def __init__(self, status_code: int, detail: str, *, original: Exception | None = None) -> None:
        self.status_code = status_code
        self.detail = detail
        self.original = original
        super().__init__(detail)


def raise_confirmation_http_exception(exc: ConfirmationError) -> NoReturn:
    raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc.original


def _confirm_pending_inpost(
    draft_id: str,
    draft: dict[str, Any],
    repository: execution_workflow.ExecutionRepository,
) -> ConfirmationResult:
    if not inpost_provider.is_resumable_inpost_draft(draft):
        raise ConfirmationError(409, "Draft has no courier_draft_id to confirm")
    if MOCK_COURIER:
        patch = {
            "status": "created",
            "tracking_number": f"MOCK{draft.get('shopify_order_number', 'x')}0000000000",
            "error": None,
        }
        repository.update_draft(draft_id, patch)
        return ConfirmationResult(repository.get_draft(draft_id) or patch)

    from zdrovena.common.inpost import InPostClient

    client = InPostClient(get_secret("inpost_api_token"), get_secret("inpost_organization_id"))
    try:
        patch = inpost_provider.resume_inpost_shipment(
            client,
            draft,
            build_patch=shipment_patch,
        )
    except (InPostBusinessError, CourierTransientError) as exc:
        logger.exception("InPost confirm poll failed for draft %s", draft_id)
        raise ConfirmationError(502, f"InPost API error: {exc}", original=exc) from exc
    if patch.get("status") != "created":
        return ConfirmationResult(
            {
                "status": "pending_confirmation",
                "courier_draft_id": patch.get("courier_draft_id"),
                "draft_id": draft_id,
            },
            status_code=202,
        )
    patch["shipment_origin"] = SHIPMENT_ORIGIN_SYSTEM
    repository.update_draft(draft_id, patch)
    emit_tracking_assigned(
        draft_id,
        draft.get("shopify_order_number"),
        SHIPMENT_ORIGIN_SYSTEM,
    )
    return ConfirmationResult(repository.get_draft(draft_id) or patch)


def confirm_shipping_draft(
    draft_id: str,
    repository: execution_workflow.ExecutionRepository,
) -> ConfirmationResult:
    """Resolve one pending InPost or Allegro shipment command."""
    draft = repository.get_draft(draft_id)
    if not draft:
        raise ConfirmationError(404, "Draft not found")
    if draft.get("status") != "pending_confirmation":
        raise ConfirmationError(409, "Draft is not pending confirmation")

    command_id = draft.get("allegro_command_id") or next(
        (
            shipment.get("allegro_command_id")
            for shipment in draft.get("courier_shipments") or []
            if shipment.get("allegro_command_id") and not shipment.get("id")
        ),
        None,
    )
    if not command_id:
        if draft.get("courier") == "inpost":
            return _confirm_pending_inpost(draft_id, draft, repository)
        raise ConfirmationError(409, "Draft has no allegro_command_id")

    if MOCK_COURIER:
        patch = {
            "status": "created",
            "courier_draft_id": f"mock-allegro-{draft.get('shopify_order_number', 'x')}",
            "tracking_number": "AWA00000000",
            "error": None,
        }
        repository.update_draft(draft_id, patch)
        return ConfirmationResult(repository.get_draft(draft_id) or patch)

    persisted_shipments = [dict(item) for item in draft.get("courier_shipments") or []]

    def persist_command(created_command_id: str) -> None:
        repository.update_draft(
            draft_id,
            {
                "allegro_command_id": created_command_id,
                "status": "pending_confirmation",
                "error": None,
            },
        )

    def persist_shipment(shipment: dict[str, str]) -> None:
        key = (shipment.get("package_type"), shipment.get("package_number"))
        for index, existing in enumerate(persisted_shipments):
            if (existing.get("package_type"), existing.get("package_number")) == key:
                persisted_shipments[index] = shipment
                break
        else:
            persisted_shipments.append(shipment)
        repository.update_draft(draft_id, {"courier_shipments": persisted_shipments})

    try:
        patch = _run_allegro_delivery(
            draft,
            on_command_created=persist_command,
            on_shipment_checkpoint=persist_shipment,
        )
    except (AllegroAuthError, CourierTransientError) as exc:
        logger.exception("Confirm poll failed for draft %s", draft_id)
        raise ConfirmationError(502, f"Allegro API error: {exc}", original=exc) from exc
    except AllegroBusinessError as exc:
        logger.exception("Confirm poll failed for draft %s", draft_id)
        error_patch = {
            "status": "error",
            "error": f"Allegro create-command {command_id} failed: {exc}",
        }
        repository.update_draft(draft_id, error_patch)
        raise ConfirmationError(502, error_patch["error"], original=exc) from exc

    if patch.get("status") == "pending_confirmation":
        repository.update_draft(draft_id, patch)
        return ConfirmationResult(
            {
                "status": "pending_confirmation",
                "allegro_command_id": str(patch.get("allegro_command_id") or command_id),
                "draft_id": draft_id,
            },
            status_code=202,
        )

    patch["shipment_origin"] = SHIPMENT_ORIGIN_SYSTEM
    repository.update_draft(draft_id, patch)
    if patch.get("tracking_number"):
        emit_tracking_assigned(
            draft_id,
            draft.get("shopify_order_number"),
            SHIPMENT_ORIGIN_SYSTEM,
        )
    updated = repository.get_draft(draft_id)
    if updated:
        push_tracking_to_allegro(updated)
    return ConfirmationResult(updated or patch)
