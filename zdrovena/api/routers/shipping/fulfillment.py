"""Pickup orders and cancellations across couriers."""

from __future__ import annotations

import logging
import uuid
from typing import Annotated, Any, Literal

from fastapi import (
    APIRouter,
    Body,
    Depends,
    HTTPException,
    Response,
    status,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from zdrovena.api import shipping_execution_composition as execution_composition
from zdrovena.api.auth import Principal, require_shipment_mgr_or_above
from zdrovena.api.deps import ShippingStoreDep, StorageDep
from zdrovena.api.models import (
    ShipmentActionResponse,
)
from zdrovena.api.routers.shipping import deps
from zdrovena.common.shipping_exceptions import (
    AllegroAuthError,
    AllegroBusinessError,
    CourierAuthError,
    CourierBusinessError,
    CourierTransientError,
    ZdrovenaShippingError,
)

logger = logging.getLogger("zdrovena.api.routers.shipping.fulfillment")

router = APIRouter(tags=["shipping"])


class PickupOrderedResponse(BaseModel):
    status: Literal["pickup_ordered"]
    draft_id: str


class PickupPendingResponse(BaseModel):
    status: Literal["pickup_pending"]
    draft_id: str
    allegro_command_id: str


@router.post(
    "/shipping/drafts/{draft_id}/pickup",
    summary="Order InPost kurier pickup for an executed draft",
    response_model=PickupOrderedResponse,
    response_model_exclude_unset=True,
    responses={
        202: {"model": PickupPendingResponse, "description": "Allegro pickup still pending"},
        403: {"description": "Insufficient role"},
        404: {"description": "Draft not found"},
        409: {"description": "Pickup already ordered or draft not ready"},
        400: {"description": "Courier does not support pickup (not InPost kurier)"},
    },
)
def order_pickup(
    draft_id: str,
    shipping_store: ShippingStoreDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
    pickup_date: str | None = Body(None),
    pickup_from: str | None = Body(None),
    pickup_to: str | None = Body(None),
) -> dict[str, str] | JSONResponse:
    draft = shipping_store.get_draft(draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")
    courier = draft.get("courier")
    # Apaczka is absent on purpose: its API (service_structure / orders /
    # order_send) has no standalone pickup call. An Apaczka pickup can only be
    # requested inside the order_send payload, i.e. at execute time.
    if courier not in {"inpost", "allegro_delivery"}:
        raise HTTPException(
            status_code=400,
            detail="Pickup is only available for InPost and Ship-with-Allegro shipments",
        )
    if draft.get("status") != "created":
        raise HTTPException(status_code=409, detail="Draft must be in 'created' state")
    if draft.get("pickup_ordered"):
        raise HTTPException(status_code=409, detail="Pickup already ordered")

    shipment_ids = [
        str(shipment.get("id") or "").strip() for shipment in draft.get("courier_shipments") or []
    ]
    shipment_ids = [shipment_id for shipment_id in shipment_ids if shipment_id]
    if not shipment_ids:
        legacy_shipment_id = str(
            draft.get("allegro_shipment_id") or draft.get("courier_draft_id") or ""
        ).strip()
        shipment_ids = [legacy_shipment_id] if legacy_shipment_id else []
    if not shipment_ids:
        raise HTTPException(status_code=409, detail="No courier draft ID — execute first")

    # Claim before calling the courier (not after) so two concurrent requests
    # can't both pass the pickup_ordered check above and both dispatch.
    if not shipping_store.try_claim_pickup(draft_id):
        raise HTTPException(status_code=409, detail="Pickup already ordered")

    if execution_composition.MOCK_COURIER:
        ref = draft.get("shopify_order_number", "mock")
        logger.info("MOCK_COURIER: skipping %s pickup for draft %s", courier, ref)
    elif courier == "allegro_delivery":
        existing_command_id = str(draft.get("allegro_pickup_command_id") or "") or None

        def persist_pickup_command(command_id: str) -> None:
            shipping_store.update_draft(
                draft_id,
                {
                    "allegro_pickup_command_id": command_id,
                    "allegro_dispatch_id": None,
                    "pickup_ordered": False,
                },
            )

        try:
            allegro = execution_composition.get_allegro_client()
            if allegro is None:
                raise HTTPException(status_code=502, detail="Allegro credentials missing")
            pickup_result = execution_composition.order_allegro_pickup(
                allegro,
                shipment_ids,
                pickup_date,
                command_id=existing_command_id,
                on_command_created=persist_pickup_command,
            )
            if pickup_result["status"] == "NO_SLOT":
                raise HTTPException(
                    status_code=409,
                    detail="Allegro has no pickup slot available for this shipment",
                )
            if pickup_result["status"] == "IN_PROGRESS":
                shipping_store.update_draft(
                    draft_id,
                    {
                        "pickup_ordered": False,
                        "allegro_pickup_command_id": pickup_result["command_id"],
                        "allegro_dispatch_id": None,
                    },
                )
                return JSONResponse(
                    status_code=202,
                    content={
                        "status": "pickup_pending",
                        "draft_id": draft_id,
                        "allegro_command_id": pickup_result["command_id"],
                    },
                )
            shipping_store.update_draft(
                draft_id,
                {
                    "pickup_ordered": True,
                    "allegro_dispatch_id": pickup_result["pickup_id"],
                    "allegro_pickup_command_id": None,
                },
            )
        except HTTPException:
            shipping_store.update_draft(draft_id, {"pickup_ordered": False})
            raise
        except Exception as exc:
            logger.exception("order_pickup failed for draft %s", draft_id)
            patch: dict[str, Any] = {"pickup_ordered": False}
            if isinstance(exc, execution_composition.AllegroPickupTerminalError):
                patch["allegro_pickup_command_id"] = None
            shipping_store.update_draft(draft_id, patch)
            raise HTTPException(status_code=502, detail=f"Allegro pickup error: {exc}") from exc
    else:
        try:
            dispatch = execution_composition.order_inpost_pickup(
                draft,
                pickup_date,
                pickup_from,
                pickup_to,
            )
        except Exception as exc:
            logger.exception("order_pickup failed for draft %s", draft_id)
            shipping_store.update_draft(draft_id, {"pickup_ordered": False})
            raise HTTPException(status_code=502, detail=f"InPost dispatch error: {exc}") from exc
        # Recorded after the rollback boundary above: the dispatch already exists
        # at this point, so a storage hiccup must not release the pickup claim
        # and invite a duplicate collection. Without the id there is nothing to
        # DELETE, so the pickup could never be cancelled.
        shipping_store.update_draft(
            draft_id, {"dispatch_order_id": str(dispatch.get("id") or "") or None}
        )

    return {"status": "pickup_ordered", "draft_id": draft_id}


@router.delete(
    "/shipping/drafts/{draft_id}/shipment",
    summary="Cancel a Ship-with-Allegro shipment before dispatch",
    responses={
        403: {"description": "Insufficient role"},
        404: {"description": "Draft not found"},
        409: {"description": "No Allegro shipment to cancel"},
        502: {"description": "Allegro API error"},
    },
    response_model=ShipmentActionResponse,
    response_model_exclude_unset=True,
)
def cancel_shipment(
    draft_id: str,
    shipping_store: ShippingStoreDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
) -> dict[str, Any]:
    """Cancel the Allegro shipment created for this draft (before it is dispatched)."""
    draft = shipping_store.get_draft(draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")

    shipment_ids = [
        str(shipment.get("id") or "").strip() for shipment in draft.get("courier_shipments") or []
    ]
    shipment_ids = [shipment_id for shipment_id in shipment_ids if shipment_id]
    if not shipment_ids:
        legacy_shipment_id = str(
            draft.get("allegro_shipment_id") or draft.get("courier_draft_id") or ""
        ).strip()
        shipment_ids = [legacy_shipment_id] if legacy_shipment_id else []
    if not shipment_ids:
        raise HTTPException(status_code=409, detail="No Allegro shipment to cancel")

    if not execution_composition.MOCK_COURIER:
        client = execution_composition.get_allegro_client()
        if client is None:
            raise HTTPException(status_code=502, detail="Allegro credentials missing")
        try:
            for shipment_id in shipment_ids:
                client.cancel_ship_with_allegro_shipment(
                    command_id=str(uuid.uuid4()), shipment_id=shipment_id
                )
        except (AllegroBusinessError, AllegroAuthError, CourierTransientError) as exc:
            logger.exception("Allegro cancel shipment failed for draft %s", draft_id)
            raise HTTPException(status_code=502, detail=f"Allegro API error: {exc}") from exc

    shipping_store.update_draft(draft_id, {"status": "cancelled", "allegro_shipment_id": None})
    return {"status": "cancelled", "draft_id": draft_id, "shipment_id": shipment_ids[0]}


@router.delete(
    "/shipping/drafts/{draft_id}/dispatch",
    summary="Cancel a Ship-with-Allegro dispatch (pickup) before acceptance",
    responses={
        403: {"description": "Insufficient role"},
        404: {"description": "Draft not found"},
        409: {"description": "No Allegro dispatch to cancel"},
        502: {"description": "Allegro API error"},
    },
    response_model=ShipmentActionResponse,
    response_model_exclude_unset=True,
)
def cancel_dispatch(
    draft_id: str,
    shipping_store: ShippingStoreDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
) -> dict[str, Any]:
    """Cancel the Allegro dispatch (pickup) order created for this draft."""
    draft = shipping_store.get_draft(draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")

    dispatch_id = draft.get("allegro_dispatch_id")
    if not dispatch_id:
        raise HTTPException(status_code=409, detail="No Allegro dispatch to cancel")

    if not execution_composition.MOCK_COURIER:
        client = execution_composition.get_allegro_client()
        if client is None:
            raise HTTPException(status_code=502, detail="Allegro credentials missing")
        try:
            client.cancel_ship_with_allegro_dispatch(
                command_id=str(uuid.uuid4()), dispatch_id=str(dispatch_id)
            )
        except (AllegroBusinessError, AllegroAuthError, CourierTransientError) as exc:
            logger.exception("Allegro cancel dispatch failed for draft %s", draft_id)
            raise HTTPException(status_code=502, detail=f"Allegro API error: {exc}") from exc

    shipping_store.update_draft(draft_id, {"pickup_ordered": False, "allegro_dispatch_id": None})
    return {"status": "dispatch_cancelled", "draft_id": draft_id, "dispatch_id": str(dispatch_id)}


def _courier_cancel_http_status(exc: ZdrovenaShippingError) -> int:
    """Map a shipping-hierarchy error onto an HTTP status for cancel endpoints.

    Auth -> 401, business (e.g. already dispatched / not cancellable) -> 409,
    transient (network/5xx) -> 503, anything else in the hierarchy -> 500.
    """
    if isinstance(exc, CourierAuthError):
        return status.HTTP_401_UNAUTHORIZED
    if isinstance(exc, CourierBusinessError):
        return status.HTTP_409_CONFLICT
    if isinstance(exc, CourierTransientError):
        return status.HTTP_503_SERVICE_UNAVAILABLE
    return status.HTTP_500_INTERNAL_SERVER_ERROR


def _build_inpost_client() -> Any:
    from zdrovena.common.inpost import InPostClient

    token = deps.get_secret("inpost_api_token")
    org_id = deps.get_secret("inpost_organization_id")
    return InPostClient(token, org_id)


@router.delete(
    "/inpost/shipments/{shipment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Cancel an InPost shipment before dispatch",
    responses={
        403: {"description": "Insufficient role"},
        409: {"description": "Shipment cannot be cancelled (already dispatched / unknown)"},
        503: {"description": "InPost API transient error"},
    },
)
def cancel_inpost_shipment(
    shipment_id: str,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
) -> Response:
    if execution_composition.MOCK_COURIER:
        logger.info("MOCK_COURIER: skipping InPost cancel_shipment for %s", shipment_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    try:
        _build_inpost_client().cancel_shipment(shipment_id)
    except ZdrovenaShippingError as exc:
        logger.exception("InPost cancel_shipment failed for %s", shipment_id)
        raise HTTPException(
            status_code=_courier_cancel_http_status(exc), detail=f"InPost cancel error: {exc}"
        ) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/inpost/dispatch_orders/{dispatch_order_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Cancel an InPost dispatch order before courier acceptance",
    responses={
        403: {"description": "Insufficient role"},
        409: {"description": "Dispatch cannot be cancelled (already accepted / unknown)"},
        503: {"description": "InPost API transient error"},
    },
)
def cancel_inpost_dispatch(
    dispatch_order_id: str,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
) -> Response:
    if execution_composition.MOCK_COURIER:
        logger.info("MOCK_COURIER: skipping InPost cancel_dispatch_order for %s", dispatch_order_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    try:
        _build_inpost_client().cancel_dispatch_order(dispatch_order_id)
    except ZdrovenaShippingError as exc:
        logger.exception("InPost cancel_dispatch_order failed for %s", dispatch_order_id)
        raise HTTPException(
            status_code=_courier_cancel_http_status(exc), detail=f"InPost cancel error: {exc}"
        ) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/apaczka/orders/{order_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Cancel an Apaczka order",
    responses={
        403: {"description": "Insufficient role"},
        409: {"description": "Order cannot be cancelled (already sent / unknown)"},
        503: {"description": "Apaczka API transient error"},
    },
)
def cancel_apaczka_order(
    order_id: str,
    storage: StorageDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
) -> Response:
    if execution_composition.MOCK_COURIER:
        logger.info("MOCK_COURIER: skipping Apaczka cancel for %s", order_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    from zdrovena.common.apaczka import ApaczkaClient

    app_id = deps.get_secret("apaczka_app_id")
    app_secret = deps.get_secret("apaczka_app_secret")
    # No draft available here (only order_id) and cancel_shipment() never
    # reads service_id — pass an empty placeholder rather than looking one up.
    client = ApaczkaClient(app_id, app_secret, "", storage)
    try:
        client.cancel_shipment(order_id)
    except ZdrovenaShippingError as exc:
        logger.exception("Apaczka cancel failed for order %s", order_id)
        raise HTTPException(
            status_code=_courier_cancel_http_status(exc), detail=f"Apaczka cancel error: {exc}"
        ) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
