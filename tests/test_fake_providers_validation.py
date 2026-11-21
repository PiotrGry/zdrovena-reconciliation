"""Contract-validation coverage for the Apaczka and InPost fake providers.

Each case drives the public HTTP entry point of the emulator with a payload
that violates exactly one documented rule and asserts on the observable error
envelope (status, message, offending field).
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Iterator
from copy import deepcopy
from typing import Any

import pytest
from fastapi.testclient import TestClient

from zdrovena.common.apaczka import _sign
from zdrovena.fake_providers.app import app


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        test_client.post("/__fake__/reset").raise_for_status()
        yield test_client
        test_client.post("/__fake__/reset").raise_for_status()


# ─── Apaczka ────────────────────────────────────────────────────────────────


def _apaczka_address(**overrides: Any) -> dict[str, Any]:
    address = {
        "name": "Anna Nowak",
        "contact_person": "Anna Nowak",
        "email": "anna@example.test",
        "phone": "500500500",
        "line1": "Prosta 1",
        "city": "Warszawa",
        "postal_code": "00-001",
        "country_code": "PL",
    }
    address.update(overrides)
    return address


def _apaczka_order() -> dict[str, Any]:
    return {
        "service_id": "21",
        "address": {"sender": _apaczka_address(), "receiver": _apaczka_address()},
        "shipment": [
            {
                "weight": 1,
                "dimension1": 30,
                "dimension2": 20,
                "dimension3": 10,
                "shipment_type_code": "PACZKA",
            }
        ],
        "pickup": {"type": "COURIER"},
        "content": "Woda",
    }


def _apaczka_call(client: TestClient, endpoint: str, form: dict[str, str]) -> dict[str, Any]:
    response = client.post(f"/apaczka/api/v2/{endpoint}/", data=form)
    assert response.status_code == 200
    return response.json()


def _signed_raw(endpoint: str, request_json: str) -> dict[str, str]:
    expires = str(int(time.time()) + 600)
    message = f"fake:{endpoint}/:{request_json}:{expires}"
    signature = hmac.new(b"fake", message.encode("utf-8"), hashlib.sha256).hexdigest()
    return {"app_id": "fake", "request": request_json, "expires": expires, "signature": signature}


def test_apaczka_valid_order_is_accepted(client: TestClient) -> None:
    body = _apaczka_call(
        client, "order_send", _sign("fake", "fake", "order_send", {"order": _apaczka_order()})
    )
    assert body["status"] == 200
    assert body["response"]["order"]["status"] == "SENT"


@pytest.mark.parametrize(
    ("form_overrides", "message"),
    [
        ({"signature": ""}, "Signature fields are required"),
        ({"app_id": "someone-else"}, "Unknown app_id"),
        ({"expires": "soon"}, "Invalid expires value"),
        ({"expires": str(int(time.time()) - 60)}, "Request expired"),
        ({"expires": str(int(time.time()) + 7200)}, "Request expiry is too far in the future"),
    ],
)
def test_apaczka_rejects_invalid_authentication_fields(
    client: TestClient, form_overrides: dict[str, str], message: str
) -> None:
    form = {**_sign("fake", "fake", "service_structure", {}), **form_overrides}
    body = _apaczka_call(client, "service_structure", form)
    assert body["status"] == 401
    assert body["message"] == message


@pytest.mark.parametrize(
    ("request_json", "message"),
    [
        ("{not json", "Request is not valid JSON"),
        ("[1, 2]", "Request must be a JSON object"),
    ],
)
def test_apaczka_rejects_unparseable_request_payload(
    client: TestClient, request_json: str, message: str
) -> None:
    body = _apaczka_call(
        client, "service_structure", _signed_raw("service_structure", request_json)
    )
    assert body["status"] == 400
    assert body["message"] == message


def _mutate_order(mutation: str) -> Any:
    order = _apaczka_order()
    if mutation == "order_not_object":
        return "not-an-order"
    if mutation == "unknown_service":
        order["service_id"] = "999"
    elif mutation == "address_not_object":
        order["address"] = "Prosta 1, Warszawa"
    elif mutation == "sender_not_object":
        order["address"]["sender"] = "Zdrovena"
    elif mutation == "sender_missing_city":
        order["address"]["sender"]["city"] = "  "
    elif mutation == "receiver_bad_email":
        order["address"]["receiver"]["email"] = "anna.example.test"
    elif mutation == "receiver_bad_postal_code":
        order["address"]["receiver"]["postal_code"] = "00001"
    elif mutation == "weight_not_numeric":
        order["shipment"][0]["weight"] = "heavy"
    else:  # pragma: no cover - guards the parametrisation itself
        raise AssertionError(mutation)
    return order


@pytest.mark.parametrize(
    ("mutation", "message", "field"),
    [
        ("order_not_object", "order must be an object", "order"),
        ("unknown_service", "Unknown service_id", "order.service_id"),
        ("address_not_object", "order.address must be an object", "order.address"),
        (
            "sender_not_object",
            "order.address.sender must be an object",
            "order.address.sender",
        ),
        (
            "sender_missing_city",
            "order.address.sender.city is required",
            "order.address.sender.city",
        ),
        (
            "receiver_bad_email",
            "order.address.receiver.email is invalid",
            "order.address.receiver.email",
        ),
        (
            "receiver_bad_postal_code",
            "order.address.receiver.postal_code is invalid",
            "order.address.receiver.postal_code",
        ),
        (
            "weight_not_numeric",
            "order.shipment[0].weight must be positive",
            "order.shipment[0].weight",
        ),
    ],
)
def test_apaczka_order_send_rejects_contract_violations(
    client: TestClient, mutation: str, message: str, field: str
) -> None:
    payload = {"order": _mutate_order(mutation)}
    body = _apaczka_call(client, "order_send", _sign("fake", "fake", "order_send", payload))
    assert body["status"] == 422
    assert body["message"] == message
    assert body["response"]["errors"] == {field: [message]}
    listed = _apaczka_call(client, "orders", _sign("fake", "fake", "orders", {}))
    assert listed["response"]["orders"] == []


def test_apaczka_foreign_postal_code_format_is_not_enforced(client: TestClient) -> None:
    order = _apaczka_order()
    order["address"]["receiver"].update(country_code="DE", postal_code="10115")
    body = _apaczka_call(
        client, "order_send", _sign("fake", "fake", "order_send", {"order": order})
    )
    assert body["status"] == 200


# ─── InPost ─────────────────────────────────────────────────────────────────

_INPOST_HEADERS = {"Authorization": "Bearer fake-token"}


def _inpost_address(**overrides: Any) -> dict[str, Any]:
    address = {
        "street": "Prosta",
        "building_number": "1",
        "city": "Warszawa",
        "post_code": "00-001",
        "country_code": "PL",
    }
    address.update(overrides)
    return address


def _inpost_courier_shipment() -> dict[str, Any]:
    return {
        "service": "inpost_courier_standard",
        "receiver": {
            "first_name": "Anna",
            "last_name": "Nowak",
            "email": "anna@example.test",
            "phone": "500500500",
            "address": _inpost_address(),
        },
        "parcels": [
            {
                "dimensions": {"unit": "mm", "length": 300, "width": 200, "height": 150},
                "weight": {"unit": "kg", "amount": 6.0},
            }
        ],
    }


def _create(client: TestClient, payload: Any) -> Any:
    return client.post(
        "/inpost/v1/organizations/fake-org/shipments", headers=_INPOST_HEADERS, json=payload
    )


def test_inpost_valid_courier_shipment_is_accepted(client: TestClient) -> None:
    response = _create(client, _inpost_courier_shipment())
    assert response.status_code == 201
    assert response.json()["status"] == "created"


def _mutate_shipment(mutation: str) -> dict[str, Any]:
    shipment = _inpost_courier_shipment()
    receiver = shipment["receiver"]
    parcel = shipment["parcels"][0]
    if mutation == "receiver_not_object":
        shipment["receiver"] = "Anna Nowak"
    elif mutation == "receiver_bad_phone":
        receiver["phone"] = "call-me"
    elif mutation == "receiver_without_name":
        receiver.pop("last_name")
    elif mutation == "address_missing":
        receiver.pop("address")
    elif mutation == "address_bad_post_code":
        receiver["address"]["post_code"] = "00001"
    elif mutation == "address_bad_country":
        receiver["address"]["country_code"] = "POL"
    elif mutation == "parcels_empty":
        shipment["parcels"] = []
    elif mutation == "parcels_without_ids":
        shipment["parcels"] = [deepcopy(parcel), deepcopy(parcel)]
    elif mutation == "parcels_duplicate_ids":
        shipment["parcels"] = [{**parcel, "id": "p1"}, {**parcel, "id": "p1"}]
    elif mutation == "parcel_not_object":
        shipment["parcels"] = ["small"]
    elif mutation == "parcel_bad_template":
        shipment["parcels"] = [{"template": "huge"}]
    elif mutation == "parcel_without_dimensions":
        parcel.pop("dimensions")
    elif mutation == "dimensions_in_cm":
        parcel["dimensions"]["unit"] = "cm"
    elif mutation == "weight_in_grams":
        parcel["weight"]["unit"] = "g"
    elif mutation == "length_not_numeric":
        parcel["dimensions"]["length"] = "long"
    elif mutation == "width_too_small":
        parcel["dimensions"]["width"] = 0
    else:  # pragma: no cover - guards the parametrisation itself
        raise AssertionError(mutation)
    return shipment


@pytest.mark.parametrize(
    ("mutation", "field", "message"),
    [
        ("receiver_not_object", "receiver", "must be an object"),
        ("receiver_bad_phone", "receiver.phone", "has invalid format"),
        (
            "receiver_without_name",
            "receiver",
            "requires company_name or first_name and last_name",
        ),
        ("address_missing", "receiver.address", "must be an object"),
        ("address_bad_post_code", "receiver.address.post_code", "must use XX-XXX format"),
        ("address_bad_country", "receiver.address.country_code", "must be an ISO alpha-2 code"),
        ("parcels_empty", "parcels", "must contain at least one parcel"),
        ("parcels_without_ids", "parcels", "multi-parcel shipments require unique parcel ids"),
        ("parcels_duplicate_ids", "parcels", "multi-parcel shipments require unique parcel ids"),
        ("parcel_not_object", "parcels[0]", "must be an object"),
        ("parcel_bad_template", "parcels[0].template", "must be small, medium or large"),
        (
            "parcel_without_dimensions",
            "parcels[0]",
            "requires template or dimensions and weight",
        ),
        ("dimensions_in_cm", "parcels[0].dimensions.unit", "must be mm"),
        ("weight_in_grams", "parcels[0].weight.unit", "must be kg"),
        ("length_not_numeric", "parcels[0].dimensions.length", "must be at least 1"),
        ("width_too_small", "parcels[0].dimensions.width", "must be at least 1"),
    ],
)
def test_inpost_create_shipment_rejects_contract_violations(
    client: TestClient, mutation: str, field: str, message: str
) -> None:
    response = _create(client, _mutate_shipment(mutation))
    assert response.status_code == 422
    assert response.json() == {
        "error": "validation_failed",
        "message": message,
        "details": {field: [message]},
    }
    listed = client.get("/inpost/v1/organizations/fake-org/shipments", headers=_INPOST_HEADERS)
    assert listed.json()["count"] == 0


def test_inpost_multi_parcel_shipment_with_unique_ids_is_accepted(client: TestClient) -> None:
    shipment = _inpost_courier_shipment()
    parcel = shipment["parcels"][0]
    shipment["parcels"] = [{**parcel, "id": "p1"}, {**parcel, "id": "p2"}]
    response = _create(client, shipment)
    assert response.status_code == 201
    assert [p["id"] for p in response.json()["parcels"]] == ["p1", "p2"]


# ─── Apaczka: shipment / COD / pickup rules and non-order endpoints ────────


def _mutate_order_details(mutation: str) -> dict[str, Any]:
    order = _apaczka_order()
    shipment = order["shipment"][0]
    if mutation == "point_service_without_point":
        order["service_id"] = "14"
    elif mutation == "shipment_not_list":
        order["shipment"] = "one box"
    elif mutation == "shipment_item_not_object":
        order["shipment"] = ["box"]
    elif mutation == "shipment_missing_weight":
        shipment.pop("weight")
    elif mutation == "shipment_bad_type":
        shipment["shipment_type_code"] = "KOPERTA"
    elif mutation == "shipment_bad_nstd":
        shipment["is_nstd"] = 2
    elif mutation == "cod_not_object":
        order["cod"] = "100"
    elif mutation == "cod_amount_float":
        order["cod"] = {"amount": 1.5, "currency": "PLN"}
    elif mutation == "cod_wrong_currency":
        order["cod"] = {"amount": 100, "currency": "EUR"}
    elif mutation == "pickup_bad_type":
        order["pickup"] = {"type": "DRONE"}
    elif mutation == "pickup_bad_date":
        order["pickup"] = {"type": "COURIER", "date": "21.11.2026"}
    elif mutation == "pickup_bad_hours":
        order["pickup"] = {"type": "COURIER", "hours_from": "9am"}
    else:  # pragma: no cover - guards the parametrisation itself
        raise AssertionError(mutation)
    return order


@pytest.mark.parametrize(
    ("mutation", "field"),
    [
        ("point_service_without_point", "order.address.receiver.foreign_address_id"),
        ("shipment_not_list", "order.shipment"),
        ("shipment_item_not_object", "order.shipment[0]"),
        ("shipment_missing_weight", "order.shipment[0].weight"),
        ("shipment_bad_type", "order.shipment[0].shipment_type_code"),
        ("shipment_bad_nstd", "order.shipment[0].is_nstd"),
        ("cod_not_object", "order.cod"),
        ("cod_amount_float", "order.cod.amount"),
        ("cod_wrong_currency", "order.cod.currency"),
        ("pickup_bad_type", "order.pickup.type"),
        ("pickup_bad_date", "order.pickup.date"),
        ("pickup_bad_hours", "order.pickup.hours_from"),
    ],
)
def test_apaczka_order_send_rejects_shipment_cod_and_pickup_violations(
    client: TestClient, mutation: str, field: str
) -> None:
    payload = {"order": _mutate_order_details(mutation)}
    body = _apaczka_call(client, "order_send", _sign("fake", "fake", "order_send", payload))
    assert body["status"] == 422
    assert list(body["response"]["errors"]) == [field]


def test_apaczka_order_send_requires_order_key(client: TestClient) -> None:
    body = _apaczka_call(client, "order_send", _sign("fake", "fake", "order_send", {}))
    assert body["status"] == 422
    assert body["response"]["errors"] == {"order": ["order is required"]}


def test_apaczka_service_structure_lists_supported_services(client: TestClient) -> None:
    body = _apaczka_call(
        client, "service_structure", _sign("fake", "fake", "service_structure", {})
    )
    assert body["status"] == 200
    ids = [service["service_id"] for service in body["response"]["services"]]
    assert ids[0] == "1"
    assert "21" in ids
    assert ids == sorted(ids, key=int)


def test_apaczka_points_by_type_and_unknown_type(client: TestClient) -> None:
    body = _apaczka_call(client, "points/dpd", _sign("fake", "fake", "points/dpd", {}))
    assert body["status"] == 200
    assert body["response"]["points"]["PL-NO-COD"]["option_cod"] is False

    unknown = _apaczka_call(client, "points/ups", _sign("fake", "fake", "points/ups", {}))
    assert unknown["status"] == 404
    assert unknown["message"] == "Unknown point type: UPS"


def test_apaczka_cancel_waybill_and_unknown_endpoints(client: TestClient) -> None:
    created = _apaczka_call(
        client, "order_send", _sign("fake", "fake", "order_send", {"order": _apaczka_order()})
    )
    order_id = created["response"]["order"]["id"]

    cancel_endpoint = f"cancel_order/{order_id}"
    cancelled = _apaczka_call(client, cancel_endpoint, _sign("fake", "fake", cancel_endpoint, {}))
    assert cancelled["status"] == 200
    assert cancelled["response"]["status"] == "CANCELLED"
    assert "_detail_reads" not in cancelled["response"]

    missing_cancel = _apaczka_call(
        client, "cancel_order/nope", _sign("fake", "fake", "cancel_order/nope", {})
    )
    assert missing_cancel["status"] == 404

    missing_waybill = _apaczka_call(
        client, "waybill/nope", _sign("fake", "fake", "waybill/nope", {})
    )
    assert missing_waybill["status"] == 404
    assert missing_waybill["message"] == "Order not found"

    unsupported = _apaczka_call(client, "teleport", _sign("fake", "fake", "teleport", {}))
    assert unsupported["status"] == 404
    assert unsupported["message"] == "Unsupported endpoint: teleport"


# ─── InPost: COD, locker, sender rules and the non-create endpoints ────────


def _inpost_locker_shipment() -> dict[str, Any]:
    return {
        "service": "inpost_locker_standard",
        "receiver": {"email": "anna@example.test", "phone": "500500500"},
        "parcels": [{"template": "small"}],
        "custom_attributes": {"target_point": "WAW01"},
    }


def _mutate_inpost(mutation: str) -> Any:
    if mutation == "body_not_object":
        return ["shipment"]
    shipment = _inpost_locker_shipment()
    if mutation == "unsupported_service":
        shipment["service"] = "inpost_drone"
    elif mutation == "cod_not_object":
        shipment["cod"] = "100.00"
    elif mutation == "cod_amount_unparseable":
        shipment["cod"] = {"amount": "lots", "currency": "PLN"}
    elif mutation == "cod_amount_three_decimals":
        shipment["cod"] = {"amount": "1.005", "currency": "PLN"}
    elif mutation == "locker_receiver_not_object":
        shipment["receiver"] = "Anna"
    elif mutation == "locker_receiver_bad_phone":
        shipment["receiver"]["phone"] = "12"
    elif mutation == "locker_receiver_bad_email":
        shipment["receiver"]["email"] = "anna"
    elif mutation == "locker_without_attributes":
        shipment.pop("custom_attributes")
    elif mutation == "courier_sender_bad_email":
        shipment = _inpost_courier_shipment()
        shipment["sender"] = {
            "company_name": "Zdrovena",
            "email": "sender",
            "phone": "500500501",
            "address": _inpost_address(),
        }
    else:  # pragma: no cover - guards the parametrisation itself
        raise AssertionError(mutation)
    return shipment


@pytest.mark.parametrize(
    ("mutation", "field"),
    [
        ("body_not_object", "shipment"),
        ("unsupported_service", "shipment.service"),
        ("cod_not_object", "cod"),
        ("cod_amount_unparseable", "cod.amount"),
        ("cod_amount_three_decimals", "cod.amount"),
        ("locker_receiver_not_object", "receiver"),
        ("locker_receiver_bad_phone", "receiver.phone"),
        ("locker_receiver_bad_email", "receiver.email"),
        ("locker_without_attributes", "custom_attributes"),
        ("courier_sender_bad_email", "sender.email"),
    ],
)
def test_inpost_create_shipment_rejects_service_cod_and_party_violations(
    client: TestClient, mutation: str, field: str
) -> None:
    response = _create(client, _mutate_inpost(mutation))
    assert response.status_code == 422
    assert list(response.json()["details"]) == [field]


def test_inpost_create_shipment_requires_json_content_type(client: TestClient) -> None:
    response = client.post(
        "/inpost/v1/organizations/fake-org/shipments",
        headers={**_INPOST_HEADERS, "Content-Type": "text/plain"},
        content=b"{}",
    )
    assert response.status_code == 415


def test_inpost_organization_requires_bearer(client: TestClient) -> None:
    assert client.get("/inpost/v1/organizations/org-1").status_code == 401
    response = client.get("/inpost/v1/organizations/org-1", headers=_INPOST_HEADERS)
    assert response.status_code == 200
    assert response.json()["id"] == "org-1"


def test_inpost_shipment_lifecycle_and_unknown_ids(client: TestClient) -> None:
    shipment_id = _create(client, _inpost_locker_shipment()).json()["id"]
    shipment_url = f"/inpost/v1/shipments/{shipment_id}"
    list_url = "/inpost/v1/organizations/fake-org/shipments"

    confirmed = client.get(shipment_url, headers=_INPOST_HEADERS).json()
    assert confirmed["status"] == "confirmed"
    tracking = confirmed["tracking_number"]

    listed = client.get(
        list_url, headers=_INPOST_HEADERS, params={"tracking_number": tracking}
    ).json()
    assert [item["id"] for item in listed["items"]] == [shipment_id]
    other = client.get(list_url, headers=_INPOST_HEADERS, params={"tracking_number": "0"}).json()
    assert other["count"] == 0

    assert client.delete(shipment_url, headers=_INPOST_HEADERS).status_code == 204
    again = client.delete(shipment_url, headers=_INPOST_HEADERS)
    assert again.status_code == 422
    assert again.json()["error"] == "invalid_status"

    for method, path in [
        ("GET", "/inpost/v1/shipments/nope"),
        ("DELETE", "/inpost/v1/shipments/nope"),
        ("GET", "/inpost/v1/shipments/nope/label"),
        ("DELETE", "/inpost/v1/dispatch_orders/nope"),
    ]:
        assert client.request(method, path, headers=_INPOST_HEADERS).status_code == 404


def test_inpost_dispatch_order_validation_and_cancel(client: TestClient) -> None:
    shipment_id = _create(client, _inpost_locker_shipment()).json()["id"]
    url = "/inpost/v1/organizations/fake-org/dispatch_orders"
    address = _inpost_address()

    empty = client.post(url, headers=_INPOST_HEADERS, json={"shipments": [], "address": {}})
    assert empty.status_code == 422
    assert list(empty.json()["details"]) == ["shipments"]

    no_address = client.post(url, headers=_INPOST_HEADERS, json={"shipments": [shipment_id]})
    assert no_address.status_code == 422
    assert list(no_address.json()["details"]) == ["address"]

    unknown = client.post(
        url, headers=_INPOST_HEADERS, json={"shipments": ["nope"], "address": address}
    )
    assert unknown.status_code == 422
    assert unknown.json()["message"] == "shipment does not exist"

    created = client.post(
        url, headers=_INPOST_HEADERS, json={"shipments": [shipment_id], "address": address}
    )
    assert created.status_code == 201
    dispatch_id = created.json()["id"]
    cancelled = client.delete(f"/inpost/v1/dispatch_orders/{dispatch_id}", headers=_INPOST_HEADERS)
    assert cancelled.status_code == 204
