"""Stateful fake HTTP providers.

The app intentionally validates method, route, auth-ish headers and minimum
body shape. It is not a public API emulator; it implements the contracts used
by the real Zdrovena provider clients.
"""

from __future__ import annotations

import base64
import json
import os
import time
from copy import deepcopy
from typing import Any
from urllib.parse import parse_qs

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

PDF_BYTES = b"%PDF-1.4\n% fake provider label\n%%EOF\n"


def _sample_order(order_id: str = "fake-order-1") -> dict[str, Any]:
    return {
        "id": order_id,
        "status": "READY_FOR_PROCESSING",
        "fulfillment": {"status": "NEW"},
        "lineItems": [{"id": "line-1", "offer": {"name": "HUMIO"}, "quantity": 1}],
        "buyer": {"email": "buyer@example.test"},
        "delivery": {"method": {"name": "Fake delivery"}, "cost": {"amount": "0.00"}},
        "summary": {"totalToPay": {"amount": "29.99", "currency": "PLN"}},
    }


class FakeState:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.scenarios: dict[str, str] = {}
        self.allegro_orders: dict[str, dict[str, Any]] = {"fake-order-1": _sample_order()}
        self.allegro_shipments: dict[str, dict[str, Any]] = {}
        self.allegro_commands: dict[str, dict[str, Any]] = {}
        self.allegro_invoices: dict[str, dict[str, Any]] = {}
        self.allegro_dispatches: dict[str, dict[str, Any]] = {}
        self.inpost_shipments: dict[str, dict[str, Any]] = {}
        self.inpost_dispatches: dict[str, dict[str, Any]] = {}
        self.apaczka_orders: dict[str, dict[str, Any]] = {}
        self.fakturownia_invoices: dict[str, dict[str, Any]] = {}
        self.counters: dict[str, int] = {}

    def next_id(self, prefix: str) -> str:
        self.counters[prefix] = self.counters.get(prefix, 0) + 1
        return f"{prefix}-{self.counters[prefix]:04d}"


STATE = FakeState()
app = FastAPI(title="Zdrovena fake providers", version="1.0.0")


def _scenario_key(provider: str, operation: str) -> str:
    return f"{provider}:{operation}"


def _apply_scenario(provider: str, operation: str) -> None:
    mode = STATE.scenarios.get(_scenario_key(provider, operation))
    if mode in {"validation_error", "400"}:
        raise HTTPException(status_code=400, detail=f"{provider} {operation} validation error")
    if mode in {"422", "provider_validation_failure"}:
        raise HTTPException(status_code=422, detail=f"{provider} {operation} validation error")
    if mode in {"server_error", "500"}:
        raise HTTPException(status_code=500, detail=f"{provider} {operation} server error")
    if mode == "timeout":
        time.sleep(float(os.environ.get("FAKE_PROVIDER_TIMEOUT_SECONDS", "2")))


def _require_bearer(authorization: str | None) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Bearer token required")


def _require_basic(authorization: str | None) -> None:
    if not authorization or not authorization.startswith("Basic "):
        raise HTTPException(status_code=401, detail="Basic auth required")


def _require_json_fields(body: dict[str, Any], fields: list[str]) -> None:
    missing = [field for field in fields if field not in body]
    if missing:
        raise HTTPException(
            status_code=422, detail=f"Missing required fields: {', '.join(missing)}"
        )


async def _form_body(request: Request) -> dict[str, str]:
    raw = (await request.body()).decode("utf-8")
    return {key: values[-1] for key, values in parse_qs(raw, keep_blank_values=True).items()}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "providers": "fake"}


@app.post("/__fake__/reset")
def reset() -> dict[str, str]:
    STATE.reset()
    return {"status": "reset"}


@app.get("/__fake__/state")
def state() -> dict[str, Any]:
    return {
        "scenarios": dict(STATE.scenarios),
        "allegro": {
            "orders": deepcopy(STATE.allegro_orders),
            "shipments": deepcopy(STATE.allegro_shipments),
            "invoices": deepcopy(STATE.allegro_invoices),
        },
        "inpost": {"shipments": deepcopy(STATE.inpost_shipments)},
        "apaczka": {"orders": deepcopy(STATE.apaczka_orders)},
        "fakturownia": {"invoices": deepcopy(STATE.fakturownia_invoices)},
    }


@app.post("/__fake__/scenario")
async def scenario(request: Request) -> dict[str, str]:
    body = await request.json()
    provider = str(body.get("provider") or "")
    operation = str(body.get("operation") or "")
    mode = str(body.get("mode") or "")
    if not provider or not operation or not mode:
        raise HTTPException(status_code=422, detail="provider, operation and mode are required")
    STATE.scenarios[_scenario_key(provider, operation)] = mode
    return {"status": "ok", "key": _scenario_key(provider, operation), "mode": mode}


# ── Allegro ──────────────────────────────────────────────────────────────────


@app.post("/allegro/auth/oauth/token")
async def allegro_token(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_basic(authorization)
    _apply_scenario("allegro", "oauth_token")
    form = await _form_body(request)
    grant_type = form.get("grant_type", "")
    refresh_token = form.get("refresh_token", "")
    if grant_type != "refresh_token" or not refresh_token:
        raise HTTPException(status_code=400, detail="refresh_token grant required")
    return {
        "access_token": "fake-allegro-access-token",
        "refresh_token": "fake-allegro-refresh-token",
        "expires_in": 43200,
        "token_type": "bearer",
    }


@app.get("/allegro/order/checkout-forms")
def allegro_list_orders(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    _require_bearer(authorization)
    _apply_scenario("allegro", "list_orders")
    return {
        "checkoutForms": list(STATE.allegro_orders.values()),
        "count": len(STATE.allegro_orders),
    }


@app.get("/allegro/order/checkout-forms/{order_id}")
def allegro_get_order(
    order_id: str, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    _require_bearer(authorization)
    _apply_scenario("allegro", "get_order")
    return deepcopy(STATE.allegro_orders.get(order_id) or _sample_order(order_id))


@app.put("/allegro/order/checkout-forms/{order_id}/fulfillment")
async def allegro_fulfillment(
    order_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
) -> Response:
    _require_bearer(authorization)
    body = await request.json()
    _require_json_fields(body, ["status"])
    order = STATE.allegro_orders.setdefault(order_id, _sample_order(order_id))
    order["fulfillment"] = {"status": body["status"]}
    return Response(status_code=204)


@app.get("/allegro/order/checkout-forms/{order_id}/shipments")
def allegro_order_shipments(
    order_id: str, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    _require_bearer(authorization)
    shipments = [s for s in STATE.allegro_shipments.values() if s.get("orderId") == order_id]
    return {"shipments": shipments}


@app.post("/allegro/order/checkout-forms/{order_id}/shipments")
async def allegro_order_create_shipment(
    order_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_bearer(authorization)
    body = await request.json()
    _require_json_fields(body, ["carrierId", "waybill"])
    shipment = {"id": STATE.next_id("allegro-order-shipment"), "orderId": order_id, **body}
    STATE.allegro_shipments[shipment["id"]] = shipment
    return shipment


@app.get("/allegro/order/checkout-forms/{order_id}/invoices")
def allegro_list_invoices(
    order_id: str, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    _require_bearer(authorization)
    invoices = [i for i in STATE.allegro_invoices.values() if i.get("orderId") == order_id]
    return {"invoices": invoices}


@app.post("/allegro/order/checkout-forms/{order_id}/invoices")
async def allegro_create_invoice(
    order_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_bearer(authorization)
    _apply_scenario("allegro", "create_invoice_declaration")
    body = await request.json()
    _require_json_fields(body, ["invoiceNumber", "file"])
    if (
        STATE.scenarios.get(_scenario_key("allegro", "create_invoice_declaration"))
        == "existing_invoice"
    ):
        existing = next(
            (i for i in STATE.allegro_invoices.values() if i.get("orderId") == order_id), None
        )
        if existing:
            return existing
    invoice_id = STATE.next_id("allegro-invoice")
    invoice = {"id": invoice_id, "orderId": order_id, **body, "fileUploaded": False}
    STATE.allegro_invoices[invoice_id] = invoice
    return invoice


@app.put("/allegro/order/checkout-forms/{order_id}/invoices/{invoice_id}/file")
async def allegro_upload_invoice_file(
    order_id: str,
    invoice_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
) -> Response:
    _require_bearer(authorization)
    content = await request.body()
    if not content:
        raise HTTPException(status_code=422, detail="PDF body required")
    invoice = STATE.allegro_invoices.setdefault(invoice_id, {"id": invoice_id, "orderId": order_id})
    invoice["fileUploaded"] = True
    return Response(status_code=204)


@app.get("/allegro/shipment-management/delivery-services")
def allegro_delivery_services(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    _require_bearer(authorization)
    return {"deliveryServices": [{"id": "fake-delivery-method", "name": "Fake Allegro Delivery"}]}


@app.get("/allegro/shipment-management/delivery-proposals/{order_id}")
def allegro_delivery_proposal(
    order_id: str, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    _require_bearer(authorization)
    order = STATE.allegro_orders.get(order_id) or _sample_order(order_id)
    return {
        "orderId": order["id"],
        "receiver": {
            "name": "Fake Buyer",
            "street": "Prosta 1",
            "postalCode": "00-001",
            "city": "Warszawa",
            "countryCode": "PL",
        },
        "sender": {
            "name": "Zdrovena",
            "street": "Magazynowa 1",
            "postalCode": "00-002",
            "city": "Warszawa",
            "countryCode": "PL",
        },
    }


@app.post("/allegro/shipment-management/shipments/create-commands")
async def allegro_create_command(
    request: Request, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    _require_bearer(authorization)
    _apply_scenario("allegro", "create_command")
    body = await request.json()
    _require_json_fields(body, ["commandId", "input"])
    input_body = body["input"]
    if (
        not isinstance(input_body, dict)
        or not input_body.get("referenceNumber")
        or not input_body.get("packages")
    ):
        raise HTTPException(status_code=422, detail="referenceNumber and packages are required")
    command_id = body["commandId"]
    shipment_id = STATE.next_id("allegro-shipment")
    shipment = {
        "id": shipment_id,
        "orderId": input_body["referenceNumber"],
        "packages": [
            {
                "transportingInfo": [
                    {"carrierId": "INPOST", "carrierWaybill": f"FAKE{shipment_id[-4:]}"}
                ]
            }
        ],
        "status": "CREATED",
    }
    STATE.allegro_shipments[shipment_id] = shipment
    status = (
        "IN_PROGRESS"
        if STATE.scenarios.get(_scenario_key("allegro", "create_command")) == "pending"
        else "SUCCESS"
    )
    STATE.allegro_commands[command_id] = {
        "commandId": command_id,
        "status": status,
        "shipmentId": shipment_id,
    }
    return {"commandId": command_id, "status": status}


@app.get("/allegro/shipment-management/shipments/create-commands/{command_id}")
def allegro_command_status(
    command_id: str, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    _require_bearer(authorization)
    command = STATE.allegro_commands.get(command_id)
    if not command:
        raise HTTPException(status_code=404, detail="command not found")
    if command["status"] == "IN_PROGRESS":
        command["status"] = "SUCCESS"
    return deepcopy(command)


@app.get("/allegro/shipment-management/shipments/{shipment_id}")
def allegro_get_shipment(
    shipment_id: str, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    _require_bearer(authorization)
    shipment = STATE.allegro_shipments.get(shipment_id)
    if not shipment:
        raise HTTPException(status_code=404, detail="shipment not found")
    return deepcopy(shipment)


@app.get("/allegro/shipment-management/shipments/{shipment_id}/label")
def allegro_label(shipment_id: str, authorization: str | None = Header(default=None)) -> Response:
    _require_bearer(authorization)
    if shipment_id not in STATE.allegro_shipments:
        raise HTTPException(status_code=404, detail="shipment not found")
    return Response(PDF_BYTES, media_type="application/pdf")


@app.post("/allegro/shipment-management/pickup-proposals")
async def allegro_pickup_proposals(
    request: Request, authorization: str | None = Header(default=None)
) -> list[dict[str, Any]]:
    _require_bearer(authorization)
    body = await request.json()
    ids = body.get("input", {}).get("shipmentIds") or []
    return [
        {
            "proposals": [
                {
                    "shipmentId": shipment_id,
                    "pickupTimes": [{"date": "2026-07-16", "minTime": "08:00", "maxTime": "12:00"}],
                }
            ],
            "address": {"city": "Warszawa"},
        }
        for shipment_id in ids
    ]


@app.post("/allegro/shipment-management/pickups/create-commands")
async def allegro_pickup_command(
    request: Request, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    _require_bearer(authorization)
    body = await request.json()
    _require_json_fields(body, ["commandId", "input"])
    dispatch_id = STATE.next_id("allegro-dispatch")
    STATE.allegro_dispatches[dispatch_id] = {"id": dispatch_id, **body["input"]}
    return {"commandId": body["commandId"], "status": "SUCCESS", "dispatchId": dispatch_id}


@app.post("/allegro/shipment-management/shipments/cancel-commands")
@app.post("/allegro/shipment-management/dispatches/cancel-commands")
async def allegro_cancel_command(
    request: Request, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    _require_bearer(authorization)
    body = await request.json()
    _require_json_fields(body, ["commandId", "input"])
    return {"commandId": body["commandId"], "status": "SUCCESS"}


# ── InPost ───────────────────────────────────────────────────────────────────


@app.post("/inpost/v1/organizations/{org_id}/shipments")
async def inpost_create_shipment(
    org_id: str, request: Request, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    _require_bearer(authorization)
    _apply_scenario("inpost", "create_shipment")
    body = await request.json()
    _require_json_fields(body, ["service", "reference", "receiver", "parcels"])
    existing = next(
        (s for s in STATE.inpost_shipments.values() if s.get("reference") == body["reference"]),
        None,
    )
    if existing:
        return existing
    shipment_id = STATE.next_id("inpost-shipment")
    shipment = {
        "id": shipment_id,
        "status": "created",
        "tracking_number": f"620{shipment_id[-4:]}",
        "organization_id": org_id,
        **body,
    }
    STATE.inpost_shipments[shipment_id] = shipment
    return shipment


@app.get("/inpost/v1/shipments/{shipment_id}")
def inpost_get_shipment(
    shipment_id: str, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    _require_bearer(authorization)
    shipment = STATE.inpost_shipments.get(shipment_id)
    if not shipment:
        raise HTTPException(status_code=404, detail="shipment not found")
    return deepcopy(shipment)


@app.delete("/inpost/v1/shipments/{shipment_id}")
def inpost_cancel_shipment(
    shipment_id: str, authorization: str | None = Header(default=None)
) -> Response:
    _require_bearer(authorization)
    shipment = STATE.inpost_shipments.get(shipment_id)
    if not shipment:
        raise HTTPException(status_code=404, detail="shipment not found")
    shipment["status"] = "canceled"
    return Response(status_code=204)


@app.get("/inpost/v1/shipments/{shipment_id}/label")
def inpost_label(shipment_id: str, authorization: str | None = Header(default=None)) -> Response:
    _require_bearer(authorization)
    if STATE.scenarios.get(_scenario_key("inpost", "get_label")) == "label_not_ready":
        raise HTTPException(status_code=409, detail="label not ready")
    if shipment_id not in STATE.inpost_shipments:
        raise HTTPException(status_code=404, detail="shipment not found")
    return Response(PDF_BYTES, media_type="application/pdf")


@app.post("/inpost/v1/organizations/{org_id}/dispatch_orders")
async def inpost_create_dispatch(
    org_id: str, request: Request, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    _require_bearer(authorization)
    body = await request.json()
    _require_json_fields(body, ["shipments", "address"])
    dispatch_id = STATE.next_id("inpost-dispatch")
    dispatch = {"id": dispatch_id, "organization_id": org_id, "status": "created", **body}
    STATE.inpost_dispatches[dispatch_id] = dispatch
    return dispatch


@app.delete("/inpost/v1/organizations/{org_id}/dispatch_orders/{dispatch_id}")
def inpost_cancel_dispatch(
    org_id: str, dispatch_id: str, authorization: str | None = Header(default=None)
) -> Response:
    _require_bearer(authorization)
    dispatch = STATE.inpost_dispatches.get(dispatch_id)
    if not dispatch:
        raise HTTPException(status_code=404, detail="dispatch not found")
    dispatch["status"] = "canceled"
    return Response(status_code=204)


# ── Apaczka ──────────────────────────────────────────────────────────────────


def _parse_apaczka_request(request_payload: str) -> dict[str, Any]:
    try:
        parsed = json.loads(request_payload or "{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail="invalid Apaczka request JSON") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=422, detail="Apaczka request must be an object")
    return parsed


def _apaczka_ok(response: Any) -> dict[str, Any]:
    return {"status": 200, "message": "OK", "response": response}


@app.post("/apaczka/api/v2/{endpoint}/")
async def apaczka_call(
    endpoint: str,
    http_request: Request,
) -> dict[str, Any]:
    form = await _form_body(http_request)
    app_id = form.get("app_id", "")
    request = form.get("request", "{}")
    expires = form.get("expires", "")
    signature = form.get("signature", "")
    if not app_id or not expires or not signature:
        raise HTTPException(status_code=401, detail="Apaczka signature fields required")
    data = _parse_apaczka_request(request)
    _apply_scenario("apaczka", endpoint)
    if STATE.scenarios.get(_scenario_key("apaczka", endpoint)) == "provider_validation_failure":
        return {"status": 422, "message": "provider validation failure", "response": {}}
    if endpoint == "service_structure":
        return _apaczka_ok({"services": [{"service_id": "21", "name": "DPD Kurier"}]})
    if endpoint == "order_send":
        _require_json_fields(data, ["service_id", "order_id", "address", "shipment"])
        if data["order_id"] in STATE.apaczka_orders:
            return _apaczka_ok(STATE.apaczka_orders[data["order_id"]])
        order = {
            "id": STATE.next_id("apaczka-order"),
            "order_id": data["order_id"],
            "status": "created",
        }
        STATE.apaczka_orders[data["order_id"]] = order
        return _apaczka_ok(order)
    if endpoint == "cancel_order":
        _require_json_fields(data, ["order_id"])
        order = STATE.apaczka_orders.setdefault(data["order_id"], {"id": data["order_id"]})
        order["status"] = "cancelled"
        return _apaczka_ok(order)
    if endpoint == "waybill":
        _require_json_fields(data, ["order_id"])
        return _apaczka_ok({"waybill": base64.b64encode(PDF_BYTES).decode("ascii")})
    raise HTTPException(status_code=404, detail=f"Unsupported Apaczka endpoint: {endpoint}")


# ── Fakturownia ───────────────────────────────────────────────────────────────


def _require_fakturownia_token(api_token: str | None) -> None:
    if not api_token:
        raise HTTPException(status_code=401, detail="api_token required")


@app.get("/fakturownia/invoices.json")
def fakturownia_list(
    api_token: str | None = None, number: str | None = None, oid: str | None = None
) -> list[dict[str, Any]]:
    _require_fakturownia_token(api_token)
    invoices = list(STATE.fakturownia_invoices.values())
    if number:
        invoices = [i for i in invoices if i.get("number") == number]
    if oid:
        invoices = [i for i in invoices if i.get("oid") == oid]
    return invoices


@app.post("/fakturownia/invoices.json")
async def fakturownia_create(request: Request, api_token: str | None = None) -> dict[str, Any]:
    _require_fakturownia_token(api_token)
    _apply_scenario("fakturownia", "create_invoice")
    body = await request.json()
    invoice = body.get("invoice")
    if not isinstance(invoice, dict):
        raise HTTPException(status_code=422, detail="invoice object required")
    oid = invoice.get("oid")
    if oid:
        existing = next(
            (i for i in STATE.fakturownia_invoices.values() if i.get("oid") == oid), None
        )
        if existing:
            if (
                STATE.scenarios.get(_scenario_key("fakturownia", "create_invoice"))
                == "already_exists"
            ):
                return existing
            raise HTTPException(status_code=422, detail="invoice already exists")
    invoice_id = STATE.next_id("fakturownia-invoice")
    created = {
        "id": int(invoice_id.rsplit("-", 1)[-1]),
        "number": invoice.get("number") or invoice_id,
        **invoice,
    }
    STATE.fakturownia_invoices[str(created["id"])] = created
    return created


@app.get("/fakturownia/invoices/{invoice_id}.json")
def fakturownia_get(invoice_id: int, api_token: str | None = None) -> dict[str, Any]:
    _require_fakturownia_token(api_token)
    invoice = STATE.fakturownia_invoices.get(str(invoice_id))
    if not invoice:
        raise HTTPException(status_code=404, detail="invoice not found")
    return deepcopy(invoice)


@app.put("/fakturownia/invoices/{invoice_id}.json")
async def fakturownia_update(
    invoice_id: int, request: Request, api_token: str | None = None
) -> dict[str, Any]:
    _require_fakturownia_token(api_token)
    body = await request.json()
    patch = body.get("invoice")
    if not isinstance(patch, dict):
        raise HTTPException(status_code=422, detail="invoice object required")
    invoice = STATE.fakturownia_invoices.setdefault(str(invoice_id), {"id": invoice_id})
    invoice.update(patch)
    return deepcopy(invoice)


@app.get("/fakturownia/invoices/{invoice_id}.pdf")
def fakturownia_pdf(invoice_id: int, api_token: str | None = None) -> Response:
    _require_fakturownia_token(api_token)
    if str(invoice_id) not in STATE.fakturownia_invoices:
        raise HTTPException(status_code=404, detail="invoice not found")
    return Response(PDF_BYTES, media_type="application/pdf")


@app.exception_handler(HTTPException)
async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.get("/{path:path}")
@app.post("/{path:path}")
@app.put("/{path:path}")
@app.delete("/{path:path}")
def unsupported(path: str) -> PlainTextResponse:
    return PlainTextResponse(f"Unsupported fake provider route: /{path}", status_code=404)
