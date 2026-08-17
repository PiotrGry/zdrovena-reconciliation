"""Contract-first emulator of Apaczka Web API v2."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
from copy import deepcopy
from typing import Any

from fastapi import APIRouter, Request

from zdrovena.fake_providers.common import PDF_BYTES, apply_http_scenario, form_body
from zdrovena.fake_providers.state import STATE

router = APIRouter(prefix="/apaczka")

SUPPORTED_SERVICES = frozenset(
    {
        "1",
        "2",
        "3",
        "4",
        "14",
        "15",
        "21",
        "23",
        "24",
        "25",
        "26",
        "50",
        "53",
        "60",
        "64",
        "66",
        "82",
        "83",
        "84",
        "86",
        "151",
        "202",
        "203",
        "314",
        "317",
    }
)
POINT_SERVICES = frozenset(
    {"14", "15", "23", "26", "50", "53", "64", "66", "86", "203", "314", "317"}
)
POINTS_BY_TYPE: dict[str, dict[str, dict[str, Any]]] = {
    "DPD": {
        "PL55338": {"foreign_address_id": "PL55338", "option_cod": True},
        "PL72095": {"foreign_address_id": "PL72095", "option_cod": True},
        "PL-NO-COD": {"foreign_address_id": "PL-NO-COD", "option_cod": False},
    },
    "POCZTA": {
        "318409": {"foreign_address_id": "318409", "option_cod": True},
        "POCZTA-NO-COD": {
            "foreign_address_id": "POCZTA-NO-COD",
            "option_cod": False,
        },
    },
}
SHIPMENT_TYPES = frozenset({"PACZKA", "PALETA"})
POST_CODE_RE = re.compile(r"^[0-9]{2}-[0-9]{3}$")
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
TIME_RE = re.compile(r"^[0-2][0-9]:[0-5][0-9]$")


def _ok(response: Any) -> dict[str, Any]:
    return {"status": 200, "message": "OK", "response": response}


def _failure(status: int, message: str, *, field: str = "") -> dict[str, Any]:
    response: dict[str, Any] = {}
    if field:
        response["errors"] = {field: [message]}
    return {"status": status, "message": message, "response": response}


def _credentials() -> tuple[str, str]:
    return (
        os.environ.get("FAKE_APACZKA_APP_ID", "fake"),
        os.environ.get("FAKE_APACZKA_APP_SECRET", "fake"),
    )


def _authenticate(endpoint: str, form: dict[str, str]) -> dict[str, Any] | None:
    app_id = form.get("app_id", "")
    request_json = form.get("request", "")
    expires = form.get("expires", "")
    signature = form.get("signature", "")
    expected_app_id, secret = _credentials()
    if not app_id or not request_json or not expires or not signature:
        return _failure(401, "Signature fields are required")
    if app_id != expected_app_id:
        return _failure(401, "Unknown app_id")
    try:
        expiry = int(expires)
    except ValueError:
        return _failure(401, "Invalid expires value")
    now = int(time.time())
    if expiry < now:
        return _failure(401, "Request expired")
    if expiry > now + 3600:
        return _failure(401, "Request expiry is too far in the future")
    route = endpoint if endpoint.endswith("/") else f"{endpoint}/"
    message = f"{app_id}:{route}:{request_json}:{expires}"
    expected = hmac.new(secret.encode(), message.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return _failure(401, "Signature doesn't match")
    return None


def _parse_request(request_json: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    try:
        parsed = json.loads(request_json)
    except json.JSONDecodeError:
        return None, _failure(400, "Request is not valid JSON")
    if not isinstance(parsed, dict):
        return None, _failure(400, "Request must be a JSON object")
    return parsed, None


def _missing(data: dict[str, Any], fields: tuple[str, ...], path: str) -> dict[str, Any] | None:
    for field in fields:
        value = data.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            return _failure(422, f"{path}.{field} is required", field=f"{path}.{field}")
    return None


def _validate_address(value: Any, path: str) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return _failure(422, f"{path} must be an object", field=path)
    error = _missing(
        value,
        (
            "name",
            "contact_person",
            "email",
            "phone",
            "line1",
            "city",
            "postal_code",
            "country_code",
        ),
        path,
    )
    if error:
        return error
    if "@" not in str(value["email"]):
        return _failure(422, f"{path}.email is invalid", field=f"{path}.email")
    if value["country_code"] == "PL" and not POST_CODE_RE.fullmatch(str(value["postal_code"])):
        return _failure(422, f"{path}.postal_code is invalid", field=f"{path}.postal_code")
    return None


def _positive(value: Any) -> bool:
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def _validate_order(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return _failure(422, "order must be an object", field="order")
    error = _missing(value, ("service_id", "address", "shipment", "pickup", "content"), "order")
    if error:
        return error
    service_id = str(value["service_id"])
    if service_id not in SUPPORTED_SERVICES:
        return _failure(422, "Unknown service_id", field="order.service_id")
    address = value["address"]
    if not isinstance(address, dict):
        return _failure(422, "order.address must be an object", field="order.address")
    for role in ("sender", "receiver"):
        error = _validate_address(address.get(role), f"order.address.{role}")
        if error:
            return error
    if service_id in POINT_SERVICES and not address["receiver"].get("foreign_address_id"):
        return _failure(
            422,
            "Pickup point service requires foreign_address_id",
            field="order.address.receiver.foreign_address_id",
        )
    shipments = value["shipment"]
    if not isinstance(shipments, list) or not shipments:
        return _failure(422, "order.shipment must not be empty", field="order.shipment")
    for index, shipment in enumerate(shipments):
        path = f"order.shipment[{index}]"
        if not isinstance(shipment, dict):
            return _failure(422, f"{path} must be an object", field=path)
        error = _missing(
            shipment,
            ("weight", "dimension1", "dimension2", "dimension3", "shipment_type_code"),
            path,
        )
        if error:
            return error
        for field in ("weight", "dimension1", "dimension2", "dimension3"):
            if not _positive(shipment[field]):
                return _failure(422, f"{path}.{field} must be positive", field=f"{path}.{field}")
        if shipment["shipment_type_code"] not in SHIPMENT_TYPES:
            return _failure(
                422, f"{path}.shipment_type_code is invalid", field=f"{path}.shipment_type_code"
            )
        if shipment.get("is_nstd", 0) not in (0, 1):
            return _failure(422, f"{path}.is_nstd must be 0 or 1", field=f"{path}.is_nstd")
    cod = value.get("cod")
    if cod is not None:
        if not isinstance(cod, dict):
            return _failure(422, "order.cod must be an object", field="order.cod")
        amount = cod.get("amount")
        if type(amount) is not int or amount <= 0:
            return _failure(
                422,
                "order.cod.amount must be a positive integer in grosze",
                field="order.cod.amount",
            )
        if cod.get("currency") != "PLN":
            return _failure(422, "order.cod.currency must be PLN", field="order.cod.currency")
        if not re.fullmatch(r"[0-9]{26}", str(cod.get("bankaccount") or "")):
            return _failure(
                422,
                "order.cod.bankaccount must be a 26-digit NRB",
                field="order.cod.bankaccount",
            )
    pickup = value["pickup"]
    if not isinstance(pickup, dict) or pickup.get("type") not in {"COURIER", "SELF"}:
        return _failure(422, "order.pickup.type is invalid", field="order.pickup.type")
    if pickup.get("date") and not DATE_RE.fullmatch(str(pickup["date"])):
        return _failure(422, "order.pickup.date is invalid", field="order.pickup.date")
    for field in ("hours_from", "hours_to"):
        if pickup.get(field) and not TIME_RE.fullmatch(str(pickup[field])):
            return _failure(422, f"order.pickup.{field} is invalid", field=f"order.pickup.{field}")
    if (
        str(value.get("service_id") or "") == "23"
        and pickup.get("date")
        and (
            str(pickup.get("hours_from") or ""),
            str(pickup.get("hours_to") or ""),
        )
        not in {
            ("09:00", "17:00"),
            ("11:00", "14:00"),
            ("14:00", "17:00"),
        }
    ):
        return _failure(
            400,
            'Dozwolone przedzialy godzinowe: "[09:00|17:00,11:00|14:00,14:00|17:00]"',
            field="order.pickup",
        )
    return None


@router.post("/api/v2/{endpoint:path}/")
async def call(endpoint: str, http_request: Request) -> dict[str, Any]:
    form = await form_body(http_request)
    auth_error = _authenticate(endpoint, form)
    if auth_error:
        return auth_error
    data, parse_error = _parse_request(form["request"])
    if parse_error:
        return parse_error
    assert data is not None

    operation = endpoint.split("/", 1)[0]
    scenario = STATE.scenario("apaczka", operation)
    if scenario == "provider_validation_failure":
        return _failure(422, "provider validation failure")
    apply_http_scenario("apaczka", operation, scenario)

    if endpoint == "service_structure":
        services = [
            {"service_id": service_id, "name": f"Fake documented service {service_id}"}
            for service_id in sorted(SUPPORTED_SERVICES, key=int)
        ]
        return _ok({"services": services})
    if endpoint.startswith("points/"):
        point_type = endpoint.removeprefix("points/").upper()
        points = POINTS_BY_TYPE.get(point_type)
        if points is None:
            return _failure(404, f"Unknown point type: {point_type}")
        return _ok({"points": deepcopy(points)})
    if endpoint == "orders":
        return _ok({"orders": [deepcopy(order) for order in STATE.apaczka_orders.values()]})
    if endpoint == "order_send":
        if "order" not in data:
            return _failure(422, "order is required", field="order")
        contract_error = _validate_order(data["order"])
        if contract_error:
            return contract_error
        order_data = data["order"]
        order_id = STATE.next_id("apaczka-order")
        created = {
            "id": order_id,
            "externalId": str(order_data.get("externalId") or ""),
            "status": "SENT",
            "waybill_number": f"APZ{order_id[-4:]}000000",
            "service_id": str(order_data["service_id"]),
        }
        STATE.apaczka_orders[order_id] = created
        return _ok({"order": deepcopy(created)})
    if endpoint.startswith("cancel_order/"):
        order_id = endpoint.removeprefix("cancel_order/")
        order = STATE.apaczka_orders.get(order_id)
        if not order:
            return _failure(404, "Order not found")
        order["status"] = "CANCELLED"
        return _ok(deepcopy(order))
    if endpoint.startswith("waybill/"):
        order_id = endpoint.removeprefix("waybill/")
        if order_id not in STATE.apaczka_orders:
            return _failure(404, "Order not found")
        return _ok({"waybill": base64.b64encode(PDF_BYTES).decode("ascii")})
    return _failure(404, f"Unsupported endpoint: {endpoint}")
