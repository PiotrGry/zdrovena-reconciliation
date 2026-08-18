"""Contract-first emulator of the ShipX v1 endpoints used by Zdrovena."""

from __future__ import annotations

import re
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from typing import Any, NoReturn

from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from zdrovena.fake_providers.common import PDF_BYTES, apply_http_scenario, require_bearer
from zdrovena.fake_providers.state import STATE

router = APIRouter(prefix="/inpost")

SERVICES = frozenset(
    {
        "inpost_locker_standard",
        "inpost_courier_standard",
        "inpost_courier_c2c",
    }
)
LOCKER_TEMPLATES = frozenset({"small", "medium", "large"})
PHONE_RE = re.compile(r"^\+?[0-9]{9,15}$")
POST_CODE_RE = re.compile(r"^[0-9]{2}-[0-9]{3}$")


class ContractViolation(ValueError):
    def __init__(self, field: str, message: str) -> None:
        self.field = field
        self.message = message
        super().__init__(f"{field}: {message}")


def _reject(field: str, message: str) -> NoReturn:
    raise ContractViolation(field, message)


def _error(status: int, code: str, message: str, field: str = "") -> JSONResponse:
    details = {field: [message]} if field else {}
    return JSONResponse(
        status_code=status,
        content={"error": code, "message": message, "details": details},
    )


def _required(data: dict[str, Any], field: str, path: str) -> Any:
    value = data.get(field)
    if value is None or (isinstance(value, str) and not value.strip()):
        _reject(f"{path}.{field}", "is required")
    return value


def _address(value: Any, path: str) -> None:
    if not isinstance(value, dict):
        _reject(path, "must be an object")
    for field in ("street", "building_number", "city", "post_code"):
        _required(value, field, path)
    if not POST_CODE_RE.fullmatch(str(value["post_code"])):
        _reject(f"{path}.post_code", "must use XX-XXX format")
    country = value.get("country_code", "PL")
    if not isinstance(country, str) or len(country) != 2:
        _reject(f"{path}.country_code", "must be an ISO alpha-2 code")


def _person(value: Any, path: str, *, address_required: bool) -> dict[str, Any]:
    if not isinstance(value, dict):
        _reject(path, "must be an object")
    phone = str(_required(value, "phone", path))
    if not PHONE_RE.fullmatch(phone.replace(" ", "")):
        _reject(f"{path}.phone", "has invalid format")
    if not value.get("company_name") and not (value.get("first_name") and value.get("last_name")):
        _reject(path, "requires company_name or first_name and last_name")
    if address_required:
        _address(value.get("address"), f"{path}.address")
    return value


def _positive(value: Any, path: str) -> None:
    try:
        valid = float(value) >= 1
    except (TypeError, ValueError):
        valid = False
    if not valid:
        _reject(path, "must be at least 1")


def _validate_parcels(value: Any) -> None:
    if not isinstance(value, list) or not value:
        _reject("parcels", "must contain at least one parcel")
    if len(value) > 1:
        ids = [parcel.get("id") for parcel in value if isinstance(parcel, dict)]
        if len(ids) != len(value) or not all(ids) or len(set(ids)) != len(ids):
            _reject("parcels", "multi-parcel shipments require unique parcel ids")
    for index, parcel in enumerate(value):
        path = f"parcels[{index}]"
        if not isinstance(parcel, dict):
            _reject(path, "must be an object")
        template = parcel.get("template")
        if template is not None:
            if template not in LOCKER_TEMPLATES:
                _reject(f"{path}.template", "must be small, medium or large")
            continue
        dimensions = parcel.get("dimensions")
        weight = parcel.get("weight")
        if not isinstance(dimensions, dict) or not isinstance(weight, dict):
            _reject(path, "requires template or dimensions and weight")
        if dimensions.get("unit") != "mm":
            _reject(f"{path}.dimensions.unit", "must be mm")
        if weight.get("unit") != "kg":
            _reject(f"{path}.weight.unit", "must be kg")
        for field in ("length", "width", "height"):
            _positive(dimensions.get(field), f"{path}.dimensions.{field}")
        _positive(weight.get("amount"), f"{path}.weight.amount")


def _validate_cod(value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        _reject("cod", "must be an object")
    try:
        amount = Decimal(str(value.get("amount")))
    except (InvalidOperation, ValueError):
        _reject("cod.amount", "must be a positive amount with two decimal places")
    if not amount.is_finite() or amount <= 0 or amount != amount.quantize(Decimal("0.01")):
        _reject("cod.amount", "must be a positive amount with two decimal places")
    if value.get("currency") != "PLN":
        _reject("cod.currency", "must be PLN")


def _validate_insurance(cod: Any, insurance: Any) -> None:
    """Mirror the live ShipX rule that broke production order #1708.

    A COD shipment must declare insurance at least equal to the collected
    amount; ShipX answers 400 validation_failed with
    `{"insurance": ["should_be_greater_or_equal_than_cod"]}` otherwise.
    """
    if not isinstance(cod, dict):
        return
    if not isinstance(insurance, dict):
        _reject("insurance", "should_be_greater_or_equal_than_cod")
    try:
        insured = Decimal(str(insurance.get("amount")))
        collected = Decimal(str(cod.get("amount")))
    except (InvalidOperation, ValueError):
        _reject("insurance", "should_be_greater_or_equal_than_cod")
    if not insured.is_finite() or insured < collected:
        _reject("insurance", "should_be_greater_or_equal_than_cod")
    if insurance.get("currency") != cod.get("currency"):
        _reject("insurance.currency", "must match cod.currency")


def validate_shipment(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        _reject("shipment", "must be an object")
    service = str(_required(body, "service", "shipment"))
    if service not in SERVICES:
        _reject("shipment.service", "is not supported")
    _validate_parcels(body.get("parcels"))
    _validate_cod(body.get("cod"))
    _validate_insurance(body.get("cod"), body.get("insurance"))
    receiver = body.get("receiver")
    if "locker" in service:
        if not isinstance(receiver, dict):
            _reject("receiver", "must be an object")
        phone = str(_required(receiver, "phone", "receiver"))
        if not PHONE_RE.fullmatch(phone.replace(" ", "")):
            _reject("receiver.phone", "has invalid format")
        email = str(_required(receiver, "email", "receiver"))
        if "@" not in email:
            _reject("receiver.email", "has invalid format")
        attributes = body.get("custom_attributes")
        if not isinstance(attributes, dict):
            _reject("custom_attributes", "must be an object")
        _required(attributes, "target_point", "custom_attributes")
    else:
        _person(receiver, "receiver", address_required=True)
        sender = body.get("sender")
        if sender is not None:
            sender_data = _person(sender, "sender", address_required=True)
            email = str(_required(sender_data, "email", "sender"))
            if "@" not in email:
                _reject("sender.email", "has invalid format")
    return body


def _require_json(content_type: str | None) -> None:
    if not content_type or content_type.split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(status_code=415, detail="Content-Type must be application/json")


@router.get("/v1/organizations/{org_id}")
def organization(
    org_id: str,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_bearer(authorization)
    return {"id": org_id, "name": "Fake ShipX Organization", "status": "active"}


@router.post("/v1/organizations/{org_id}/shipments", status_code=201)
async def create_shipment(
    org_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
    content_type: str | None = Header(default=None),
) -> Response:
    require_bearer(authorization)
    _require_json(content_type)
    scenario = STATE.scenario("inpost", "create_shipment")
    apply_http_scenario("inpost", "create_shipment", scenario)
    try:
        body = validate_shipment(await request.json())
    except ContractViolation as exc:
        return _error(422, "validation_failed", exc.message, exc.field)
    shipment_id = STATE.next_id("inpost-shipment")
    shipment = {
        "id": shipment_id,
        "status": "created",
        "tracking_number": None,
        "organization_id": org_id,
        "created_at": "2026-08-03T10:00:00.000Z",
        "updated_at": "2026-08-03T10:00:00.000Z",
        "_polls": 0,
        **deepcopy(body),
    }
    STATE.inpost_shipments[shipment_id] = shipment
    public = {key: value for key, value in shipment.items() if not key.startswith("_")}
    return JSONResponse(status_code=201, content=public)


@router.get("/v1/organizations/{org_id}/shipments")
def list_shipments(
    org_id: str,
    tracking_number: str | None = None,
    per_page: int = 25,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_bearer(authorization)
    values = [s for s in STATE.inpost_shipments.values() if s["organization_id"] == org_id]
    if tracking_number:
        values = [s for s in values if s.get("tracking_number") == tracking_number]
    items = [
        {key: value for key, value in deepcopy(shipment).items() if not key.startswith("_")}
        for shipment in values[:per_page]
    ]
    return {"href": f"/v1/organizations/{org_id}/shipments", "count": len(items), "items": items}


@router.get("/v1/shipments/{shipment_id}")
def get_shipment(
    shipment_id: str,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_bearer(authorization)
    shipment = STATE.inpost_shipments.get(shipment_id)
    if not shipment:
        raise HTTPException(status_code=404, detail="shipment not found")
    shipment["_polls"] += 1
    if shipment["status"] == "created" and STATE.scenario("inpost", "create_shipment") != "pending":
        shipment["status"] = "confirmed"
        shipment["tracking_number"] = f"6200000000000000000{shipment_id[-4:]}"
        shipment["updated_at"] = "2026-08-03T10:00:01.000Z"
    return {key: value for key, value in deepcopy(shipment).items() if not key.startswith("_")}


@router.delete("/v1/shipments/{shipment_id}", status_code=204)
def cancel_shipment(
    shipment_id: str,
    authorization: str | None = Header(default=None),
) -> Response:
    require_bearer(authorization)
    shipment = STATE.inpost_shipments.get(shipment_id)
    if not shipment:
        raise HTTPException(status_code=404, detail="shipment not found")
    if shipment["status"] not in {"created", "confirmed"}:
        return _error(422, "invalid_status", "shipment cannot be cancelled")
    shipment["status"] = "canceled"
    return Response(status_code=204)


@router.get("/v1/shipments/{shipment_id}/label")
def label(
    shipment_id: str,
    authorization: str | None = Header(default=None),
) -> Response:
    require_bearer(authorization)
    shipment = STATE.inpost_shipments.get(shipment_id)
    if not shipment:
        raise HTTPException(status_code=404, detail="shipment not found")
    if (
        STATE.scenario("inpost", "get_label") == "label_not_ready"
        or shipment["status"] != "confirmed"
    ):
        return _error(409, "label_not_ready", "label is not ready")
    return Response(PDF_BYTES, media_type="application/pdf")


@router.post("/v1/organizations/{org_id}/dispatch_orders", status_code=201)
async def create_dispatch(
    org_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
    content_type: str | None = Header(default=None),
) -> Response:
    require_bearer(authorization)
    _require_json(content_type)
    body = await request.json()
    shipments = body.get("shipments")
    address = body.get("address")
    if not isinstance(shipments, list) or not shipments:
        return _error(422, "validation_failed", "shipments must not be empty", "shipments")
    if not isinstance(address, dict):
        return _error(422, "validation_failed", "address is required", "address")
    unknown = [
        shipment_id for shipment_id in shipments if shipment_id not in STATE.inpost_shipments
    ]
    if unknown:
        return _error(422, "validation_failed", "shipment does not exist", "shipments")
    dispatch_id = STATE.next_id("inpost-dispatch")
    dispatch = {
        "id": dispatch_id,
        "organization_id": org_id,
        "status": "created",
        **deepcopy(body),
    }
    STATE.inpost_dispatches[dispatch_id] = dispatch
    return JSONResponse(status_code=201, content=dispatch)


@router.delete("/v1/dispatch_orders/{dispatch_id}", status_code=204)
def cancel_dispatch(
    dispatch_id: str,
    authorization: str | None = Header(default=None),
) -> Response:
    require_bearer(authorization)
    dispatch = STATE.inpost_dispatches.get(dispatch_id)
    if not dispatch:
        raise HTTPException(status_code=404, detail="dispatch not found")
    dispatch["status"] = "canceled"
    return Response(status_code=204)
