"""Golden-master contract test for POST /shipment-management/shipments/create-commands.

Loads the real request contract captured in
``docs/audit/fixtures/allegro_create_commands_request.json`` and asserts that
``AllegroClient.create_ship_with_allegro_shipment`` reproduces the exact payload
shape Allegro expects. This is the regression guard for the 4 contract bugs that
made "Wysyłam z Allegro" return 400 Bad Request:

  - order_id must be sent as ``referenceNumber`` (no top-level ``orderId``)
  - packages use FLAT ``length``/``width``/``height`` (no ``dimensions`` wrapper)
  - weight uses ``.value`` and the plural unit ``KILOGRAMS``
  - each package carries ``type: "PACKAGE"``
  - ``additionalServices`` is an Array of strings (not a dict)
  - pickup point lives in ``receiver.point`` (no top-level ``pickupPointId``)
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from zdrovena.common.allegro import AllegroClient

_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "audit"
    / "fixtures"
    / "allegro_create_commands_request.json"
)


def _load_fixture() -> dict:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def _mock_client() -> AllegroClient:
    c = AllegroClient(client_id="cid", client_secret="csec", refresh_token="rt", env="prod")
    c._access_token = "test-token"
    c._expires_at = 9999999999
    return c


def _mock_response(status: int = 201, json_data=None):
    resp = MagicMock()
    resp.status_code = status
    resp.text = "" if json_data is None else str(json_data)
    resp.json.return_value = json_data if json_data is not None else {}
    return resp


def _capture_body() -> dict:
    """Call create_ship_with_allegro_shipment with the fixture's data, return posted body."""
    fixture = _load_fixture()
    fx_input = fixture["input"]
    c = _mock_client()
    with patch.object(
        c._session,
        "request",
        return_value=_mock_response(201, {"commandId": fixture["commandId"]}),
    ) as m:
        c.create_ship_with_allegro_shipment(
            command_id=fixture["commandId"],
            order_id=fx_input["referenceNumber"],
            delivery_method_id=fx_input["deliveryMethodId"],
            credentials_id=fx_input["credentialsId"],
            packages=fx_input["packages"],
            sender=fx_input["sender"],
            receiver=fx_input["receiver"],
            additional_services=fx_input["additionalServices"],
        )
    return m.call_args[1]["json"]


class TestCreateCommandsGoldenMaster:
    def test_command_wrapper(self):
        fixture = _load_fixture()
        body = _capture_body()
        assert body["commandId"] == fixture["commandId"]
        assert "input" in body

    def test_reference_number_replaces_order_id(self):
        fixture = _load_fixture()
        body = _capture_body()
        assert body["input"]["referenceNumber"] == fixture["input"]["referenceNumber"]
        assert "orderId" not in body["input"]

    def test_sender_and_receiver_blocks_match_fixture(self):
        fixture = _load_fixture()
        body = _capture_body()
        assert body["input"]["sender"] == fixture["input"]["sender"]
        assert body["input"]["receiver"] == fixture["input"]["receiver"]
        # Pickup point is inside receiver, never top-level.
        assert "pickupPointId" not in body["input"]
        assert body["input"]["receiver"]["point"] == fixture["input"]["receiver"]["point"]

    def test_packages_match_fixture_contract(self):
        fixture = _load_fixture()
        body = _capture_body()
        pkg = body["input"]["packages"][0]
        fx_pkg = fixture["input"]["packages"][0]
        assert pkg["type"] == "PACKAGE"
        # FLAT dims — no `dimensions` wrapper.
        assert "dimensions" not in pkg
        assert pkg["length"] == fx_pkg["length"]
        assert pkg["width"] == fx_pkg["width"]
        assert pkg["height"] == fx_pkg["height"]
        # Weight uses `.value` and the plural unit.
        assert pkg["weight"]["value"] == fx_pkg["weight"]["value"]
        assert pkg["weight"]["unit"] == "KILOGRAMS"

    def test_additional_services_is_array_of_strings(self):
        fixture = _load_fixture()
        body = _capture_body()
        services = body["input"]["additionalServices"]
        assert isinstance(services, list)
        assert all(isinstance(s, str) for s in services)
        assert services == fixture["input"]["additionalServices"]

    def test_credentials_id_passed_through(self):
        fixture = _load_fixture()
        body = _capture_body()
        assert body["input"]["credentialsId"] == fixture["input"]["credentialsId"]

    def test_delivery_method_id_passed_through(self):
        fixture = _load_fixture()
        body = _capture_body()
        assert body["input"]["deliveryMethodId"] == fixture["input"]["deliveryMethodId"]
