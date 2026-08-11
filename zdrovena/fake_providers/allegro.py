"""Contract-first emulator for the Allegro APIs used by Zdrovena.

Shipment-management behavior follows Allegro's asynchronous command model:
POST accepts a command, while GET exposes IN_PROGRESS and only later creates
the shipment resource. The emulator is intentionally independent from
``AllegroClient`` payload builders.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from zdrovena.fake_providers.common import (
    PDF_BYTES,
    apply_http_scenario,
    form_body,
    require_basic,
    require_bearer,
    require_fields,
    require_non_empty,
)
from zdrovena.fake_providers.state import STATE, sample_allegro_order

router = APIRouter(prefix="/allegro")

MEDIA_TYPE = "application/vnd.allegro.public.v1+json"
PACKAGE_TYPES = frozenset({"PACKAGE", "DOX", "PALLET"})
DIMENSION_UNITS = frozenset({"MILLIMETER", "CENTIMETER", "METER"})
WEIGHT_UNITS = frozenset({"KILOGRAMS"})
INPOST_SENDING_METHODS = frozenset({"parcel_locker", "dispatch_order", "pop", "any_point"})


def _require_headers(
    authorization: str | None,
    accept: str | None,
    content_type: str | None = None,
) -> None:
    require_bearer(authorization)
    if accept != MEDIA_TYPE:
        raise HTTPException(status_code=406, detail=f"Accept must be {MEDIA_TYPE}")
    if content_type is not None and content_type != MEDIA_TYPE:
        raise HTTPException(status_code=415, detail=f"Content-Type must be {MEDIA_TYPE}")


def _validate_address(value: Any, path: str, *, receiver: bool = False) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HTTPException(status_code=422, detail=f"{path} must be an object")
    if not value:
        raise HTTPException(status_code=422, detail=f"{path} must not be empty")
    if receiver:
        require_non_empty(value, ("email",), path)
    country = value.get("countryCode")
    if country is not None and (not isinstance(country, str) or len(country) != 2):
        raise HTTPException(status_code=422, detail=f"{path}.countryCode must be ISO alpha-2")
    return value


def _positive_measure(value: Any, path: str, units: frozenset[str]) -> None:
    if not isinstance(value, dict):
        raise HTTPException(status_code=422, detail=f"{path} must be an object")
    require_fields(value, ("value", "unit"), path)
    try:
        positive = float(value["value"]) > 0
    except (TypeError, ValueError):
        positive = False
    if not positive:
        raise HTTPException(status_code=422, detail=f"{path}.value must be positive")
    if value["unit"] not in units:
        raise HTTPException(status_code=422, detail=f"Unsupported {path}.unit")


def _validate_create_input(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HTTPException(status_code=422, detail="input must be an object")
    require_fields(value, ("sender", "receiver", "packages"), "input")
    _validate_address(value["sender"], "input.sender")
    _validate_address(value["receiver"], "input.receiver", receiver=True)
    packages = value["packages"]
    if not isinstance(packages, list) or not packages:
        raise HTTPException(status_code=422, detail="input.packages must not be empty")
    for index, package in enumerate(packages):
        path = f"input.packages[{index}]"
        if not isinstance(package, dict):
            raise HTTPException(status_code=422, detail=f"{path} must be an object")
        require_non_empty(package, ("type",), path)
        if package["type"] not in PACKAGE_TYPES:
            raise HTTPException(status_code=422, detail=f"Unsupported {path}.type")
        for name in ("length", "width", "height"):
            if name in package:
                _positive_measure(package[name], f"{path}.{name}", DIMENSION_UNITS)
        if "weight" in package:
            _positive_measure(package["weight"], f"{path}.weight", WEIGHT_UNITS)
    properties = value.get("additionalProperties")
    if properties is not None:
        if not isinstance(properties, dict):
            raise HTTPException(status_code=422, detail="input.additionalProperties must be object")
        method = properties.get("inpost#sendingMethod")
        if method is not None and method not in INPOST_SENDING_METHODS:
            raise HTTPException(status_code=422, detail="Invalid inpost#sendingMethod")
    return value


def _create_completed_shipment(command: dict[str, Any]) -> str:
    existing_id = command.get("shipmentId")
    if existing_id:
        return str(existing_id)
    shipment_id = STATE.next_id("allegro-shipment")
    input_body = command["input"]
    shipment = {
        "id": shipment_id,
        "referenceNumber": input_body.get("referenceNumber"),
        "status": "CREATED",
        "packages": [
            {
                **deepcopy(package),
                "id": STATE.next_id("allegro-package"),
                "transportingInfo": [
                    {
                        "carrierId": "INPOST",
                        "carrierWaybill": f"620000000000{shipment_id[-4:]}",
                    }
                ],
            }
            for package in input_body["packages"]
        ],
    }
    STATE.allegro_shipments[shipment_id] = shipment
    command["shipmentId"] = shipment_id
    return shipment_id


@router.post("/auth/oauth/token")
async def token(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_basic(authorization)
    apply_http_scenario("allegro", "oauth_token", STATE.scenario("allegro", "oauth_token"))
    form = await form_body(request)
    if form.get("grant_type") != "refresh_token" or not form.get("refresh_token"):
        raise HTTPException(status_code=400, detail="refresh_token grant required")
    return {
        "access_token": "fake-allegro-access-token",
        "refresh_token": "fake-allegro-refresh-token",
        "expires_in": 43200,
        "token_type": "bearer",
    }


@router.get("/order/checkout-forms")
def list_orders(
    authorization: str | None = Header(default=None),
    accept: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_headers(authorization, accept)
    apply_http_scenario("allegro", "list_orders", STATE.scenario("allegro", "list_orders"))
    values = list(STATE.allegro_orders.values())
    return {"checkoutForms": deepcopy(values), "count": len(values), "totalCount": len(values)}


@router.get("/order/checkout-forms/{order_id}")
def get_order(
    order_id: str,
    authorization: str | None = Header(default=None),
    accept: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_headers(authorization, accept)
    order = STATE.allegro_orders.get(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="order not found")
    return deepcopy(order)


@router.put("/order/checkout-forms/{order_id}/fulfillment", status_code=204)
async def update_fulfillment(
    order_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
    accept: str | None = Header(default=None),
) -> Response:
    _require_headers(authorization, accept)
    body = await request.json()
    require_non_empty(body, ("status",), "fulfillment")
    order = STATE.allegro_orders.setdefault(order_id, sample_allegro_order(order_id))
    order["fulfillment"] = {"status": body["status"]}
    return Response(status_code=204)


@router.get("/order/checkout-forms/{order_id}/shipments")
def order_shipments(
    order_id: str,
    authorization: str | None = Header(default=None),
    accept: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_headers(authorization, accept)
    shipments = [
        shipment
        for shipment in STATE.allegro_shipments.values()
        if shipment.get("referenceNumber") == order_id
    ]
    return {"shipments": deepcopy(shipments)}


@router.post("/order/checkout-forms/{order_id}/shipments", status_code=201)
async def create_order_shipment(
    order_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
    accept: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_headers(authorization, accept)
    body = await request.json()
    require_non_empty(body, ("carrierId", "waybill"), "shipment")
    shipment_id = STATE.next_id("allegro-order-shipment")
    created = {"id": shipment_id, "orderId": order_id, **body}
    STATE.allegro_shipments[shipment_id] = created
    return created


@router.get("/order/checkout-forms/{order_id}/invoices")
def list_invoices(
    order_id: str,
    authorization: str | None = Header(default=None),
    accept: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_headers(authorization, accept)
    invoices = [i for i in STATE.allegro_invoices.values() if i.get("orderId") == order_id]
    return {"invoices": deepcopy(invoices)}


@router.post("/order/checkout-forms/{order_id}/invoices", status_code=201)
async def create_invoice(
    order_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
    accept: str | None = Header(default=None),
    content_type: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_headers(authorization, accept, content_type)
    body = await request.json()
    require_fields(body, ("invoiceNumber", "file"))
    invoice_id = STATE.next_id("allegro-invoice")
    created = {"id": invoice_id, "orderId": order_id, **body, "fileUploaded": False}
    STATE.allegro_invoices[invoice_id] = created
    return deepcopy(created)


@router.put("/order/checkout-forms/{order_id}/invoices/{invoice_id}/file", status_code=204)
async def upload_invoice(
    order_id: str,
    invoice_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
) -> Response:
    require_bearer(authorization)
    if not (await request.body()):
        raise HTTPException(status_code=422, detail="PDF body required")
    invoice = STATE.allegro_invoices.setdefault(invoice_id, {"id": invoice_id, "orderId": order_id})
    invoice["fileUploaded"] = True
    return Response(status_code=204)


@router.get("/shipment-management/delivery-services")
def delivery_services(
    authorization: str | None = Header(default=None),
    accept: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_headers(authorization, accept)
    return {
        "services": [
            {
                "id": "fake-delivery-method",
                "carrierId": "INPOST",
                "owner": "ALLEGRO",
                "name": "Allegro InPost",
            }
        ]
    }


@router.get("/shipment-management/delivery-proposals/{order_id}")
def delivery_proposal(
    order_id: str,
    authorization: str | None = Header(default=None),
    accept: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_headers(authorization, accept)
    order = STATE.allegro_orders.get(order_id) or sample_allegro_order(order_id)
    return {
        "orderId": order["id"],
        "suggestedInput": {
            "receiver": {
                "name": "Fake Buyer",
                "street": "Prosta 1",
                "postalCode": "00-001",
                "city": "Warszawa",
                "countryCode": "PL",
                "email": "fake+buyer@allegromail.pl",
                "phone": "500500500",
            },
            "sender": {
                "name": "Zdrovena",
                "street": "Magazynowa 1",
                "postalCode": "00-002",
                "city": "Warszawa",
                "countryCode": "PL",
                "email": "sender@example.test",
                "phone": "500500501",
            },
            "packages": [
                {
                    "type": "PACKAGE",
                    "length": {"value": 30, "unit": "CENTIMETER"},
                    "width": {"value": 20, "unit": "CENTIMETER"},
                    "height": {"value": 15, "unit": "CENTIMETER"},
                    "weight": {"value": 1, "unit": "KILOGRAMS"},
                }
            ],
        },
    }


@router.post("/shipment-management/shipments/create-commands", status_code=201)
async def create_command(
    request: Request,
    authorization: str | None = Header(default=None),
    accept: str | None = Header(default=None),
    content_type: str | None = Header(default=None),
) -> JSONResponse:
    _require_headers(authorization, accept, content_type)
    scenario = STATE.scenario("allegro", "create_command")
    apply_http_scenario("allegro", "create_command", scenario)
    body = await request.json()
    require_fields(body, ("input",))
    input_body = _validate_create_input(body["input"])
    command_id = str(body.get("commandId") or STATE.next_id("allegro-command"))
    if command_id not in STATE.allegro_commands:
        STATE.allegro_commands[command_id] = {
            "commandId": command_id,
            "input": deepcopy(input_body),
            "status": "IN_PROGRESS",
            "polls": 0,
        }
    return JSONResponse(
        status_code=201,
        content={"commandId": command_id, "input": deepcopy(input_body)},
    )


@router.get("/shipment-management/shipments/create-commands/{command_id}")
def command_status(
    command_id: str,
    response: Response,
    authorization: str | None = Header(default=None),
    accept: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_headers(authorization, accept)
    command = STATE.allegro_commands.get(command_id)
    if not command:
        raise HTTPException(status_code=404, detail="command not found")
    scenario = STATE.scenario("allegro", "create_command")
    command["polls"] += 1
    if scenario == "command_error":
        command["status"] = "ERROR"
        return {
            "commandId": command_id,
            "status": "ERROR",
            "errors": [{"code": "VALIDATION_ERROR", "message": "Shipment rejected"}],
        }
    if scenario != "pending" and command["polls"] >= 2:
        command["status"] = "SUCCESS"
        _create_completed_shipment(command)
    if command["status"] == "IN_PROGRESS":
        response.headers["Retry-After"] = "1"
        return {"commandId": command_id, "status": "IN_PROGRESS"}
    return {
        "commandId": command_id,
        "status": "SUCCESS",
        "shipmentId": command["shipmentId"],
    }


@router.get("/shipment-management/shipments/{shipment_id}")
def get_shipment(
    shipment_id: str,
    authorization: str | None = Header(default=None),
    accept: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_headers(authorization, accept)
    shipment = STATE.allegro_shipments.get(shipment_id)
    if not shipment:
        raise HTTPException(status_code=404, detail="shipment not found")
    return deepcopy(shipment)


@router.get("/shipment-management/shipments/{shipment_id}/label")
def label(
    shipment_id: str,
    authorization: str | None = Header(default=None),
    accept: str | None = Header(default=None),
) -> Response:
    _require_headers(authorization, accept)
    if shipment_id not in STATE.allegro_shipments:
        raise HTTPException(status_code=404, detail="shipment not found")
    return Response(PDF_BYTES, media_type="application/pdf")


@router.post("/shipment-management/pickup-proposals")
async def pickup_proposals(
    request: Request,
    authorization: str | None = Header(default=None),
    accept: str | None = Header(default=None),
    content_type: str | None = Header(default=None),
) -> list[dict[str, Any]]:
    _require_headers(authorization, accept, content_type)
    body = await request.json()
    require_fields(body, ("shipmentIds", "address"))
    shipment_ids = body["shipmentIds"]
    if not isinstance(shipment_ids, list) or not shipment_ids:
        raise HTTPException(status_code=422, detail="shipmentIds must not be empty")
    _validate_address(body["address"], "address")
    return [
        {
            "proposals": [
                {
                    "shipmentId": shipment_id,
                    "pickupTimes": [{"date": "2026-08-04", "minTime": "08:00", "maxTime": "16:00"}],
                }
                for shipment_id in shipment_ids
            ],
            "address": {"street": "Magazynowa 1"},
        }
    ]


@router.post("/shipment-management/pickups/create-commands", status_code=201)
async def create_pickup(
    request: Request,
    authorization: str | None = Header(default=None),
    accept: str | None = Header(default=None),
    content_type: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_headers(authorization, accept, content_type)
    body = await request.json()
    require_fields(body, ("commandId", "input"))
    pickup_input = body["input"]
    require_fields(pickup_input, ("shipmentIds", "address"), "input")
    _validate_address(pickup_input["address"], "input.address")
    if not pickup_input.get("pickupTime") and not pickup_input.get("pickupDateProposalId"):
        raise HTTPException(status_code=422, detail="input pickup time is required")
    dispatch_id = STATE.next_id("allegro-dispatch")
    STATE.allegro_dispatches[dispatch_id] = {"id": dispatch_id, **deepcopy(pickup_input)}
    return {"commandId": body["commandId"], "input": deepcopy(pickup_input)}


@router.post("/shipment-management/shipments/cancel-commands", status_code=201)
@router.post("/shipment-management/dispatches/cancel-commands", status_code=201)
async def cancel_command(
    request: Request,
    authorization: str | None = Header(default=None),
    accept: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_headers(authorization, accept)
    body = await request.json()
    require_fields(body, ("commandId", "input"))
    return {"commandId": body["commandId"], "input": deepcopy(body["input"])}
