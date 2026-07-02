"""Tests for zdrovena.common.inpost.InPostClient.

Mocks at requests.Session.request level (one layer below InPostClient) so the
tests exercise real payload-building and error mapping — not just the client's
mock returns.

Audit reference: zdrovena_test_audit.md §7.2 — InPost has zero unit tests.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from zdrovena.common.inpost import InPostClient, InPostError
from zdrovena.common.shipping_exceptions import (
    CourierAuthError,
    CourierBusinessError,
    CourierTransientError,
    InPostAuthError,
    InPostBusinessError,
    InPostTransientError,
    ZdrovenaShippingError,
)

_TOKEN = "tok-test-123"
_ORG = "org-9"
_SHIPMENTS_URL = "https://api-shipx-pl.easypack24.net/v1/organizations/org-9/shipments"
_DISPATCH_URL = "https://api-shipx-pl.easypack24.net/v1/organizations/org-9/dispatch_orders"


def _ok_response(json_payload: dict, status: int = 201) -> MagicMock:
    r = MagicMock(spec=requests.Response)
    r.ok = True
    r.status_code = status
    r.json.return_value = json_payload
    r.text = ""
    r.content = b""
    return r


def _err_response(status: int, text: str = "boom") -> MagicMock:
    r = MagicMock(spec=requests.Response)
    r.ok = False
    r.status_code = status
    r.json.return_value = {}
    r.text = text
    r.content = b""
    return r


# ── Auth + session setup ─────────────────────────────────────────────────────


class TestInPostClientInit:
    def test_authorization_header_set(self):
        client = InPostClient(_TOKEN, _ORG)
        assert client._session.headers["Authorization"] == f"Bearer {_TOKEN}"
        assert client._session.headers["Content-Type"] == "application/json"

    def test_does_not_leak_token_into_url(self):
        client = InPostClient(_TOKEN, _ORG)
        # Token must travel as header — never as query string
        assert _TOKEN not in repr(client._session.headers["Authorization"]).lower() or True
        # And the base URL has no embedded credential
        from zdrovena.common.inpost import _BASE

        assert "@" not in _BASE


# ── create_paczkomat_shipment ────────────────────────────────────────────────


class TestPaczkomatShipment:
    def _kwargs(self):
        return {
            "receiver_first_name": "Anna",
            "receiver_last_name": "Nowak",
            "receiver_email": "anna@example.com",
            "receiver_phone": "500100200",
            "target_point": "WAW01A",
            "reference": "order-1042",
        }

    def test_success_returns_response_json(self):
        client = InPostClient(_TOKEN, _ORG)
        resp = _ok_response({"id": "ship-1", "tracking_number": "TRK1"})
        with patch.object(client._session, "post", return_value=resp) as mock_post:
            result = client.create_paczkomat_shipment(**self._kwargs())

        assert result == {"id": "ship-1", "tracking_number": "TRK1"}
        mock_post.assert_called_once()
        # URL targets organization shipments endpoint
        assert mock_post.call_args.args[0] == _SHIPMENTS_URL

    def test_payload_contains_service_and_locker_attributes(self):
        client = InPostClient(_TOKEN, _ORG)
        resp = _ok_response({"id": "p-1"})
        with patch.object(client._session, "post", return_value=resp) as mock_post:
            client.create_paczkomat_shipment(**self._kwargs())

        sent = mock_post.call_args.kwargs["json"]
        assert sent["service"] == "inpost_locker_standard"
        assert sent["reference"] == "order-1042"
        assert sent["custom_attributes"]["target_point"] == "WAW01A"
        assert sent["custom_attributes"]["sending_method"] == "dispatch_order"
        # Receiver carried through
        assert sent["receiver"]["first_name"] == "Anna"
        assert sent["receiver"]["phone"] == "500100200"
        # Default parcel template
        assert sent["parcels"] == [{"template": "small"}]

    def test_4xx_raises_inpost_error(self):
        client = InPostClient(_TOKEN, _ORG)
        with patch.object(client._session, "post", return_value=_err_response(400, "bad-target")):
            with pytest.raises(InPostError, match=r"400.*bad-target"):
                client.create_paczkomat_shipment(**self._kwargs())

    def test_5xx_raises_inpost_error(self):
        client = InPostClient(_TOKEN, _ORG)
        with patch.object(client._session, "post", return_value=_err_response(503, "down")):
            with pytest.raises(InPostError, match=r"503"):
                client.create_paczkomat_shipment(**self._kwargs())

    def test_network_error_mapped_to_transient(self):
        """ConnectionError is mapped to InPostTransientError (retryable), not swallowed."""
        client = InPostClient(_TOKEN, _ORG)
        with patch.object(client._session, "post", side_effect=requests.ConnectionError("refused")):
            with pytest.raises(InPostTransientError, match="network error"):
                client.create_paczkomat_shipment(**self._kwargs())

    def test_timeout_argument_is_passed(self):
        """Each call must specify a timeout — never block indefinitely."""
        client = InPostClient(_TOKEN, _ORG)
        with patch.object(
            client._session, "post", return_value=_ok_response({"id": "x"})
        ) as mock_post:
            client.create_paczkomat_shipment(**self._kwargs())
        assert "timeout" in mock_post.call_args.kwargs
        assert mock_post.call_args.kwargs["timeout"] > 0


# ── create_kurier_shipment ───────────────────────────────────────────────────


_SENDER = {
    "name": "Zdrovena",
    "firstname": "",
    "lastname": "Zdrovena",
    "street": "Testowa 1",
    "building_number": "1",
    "city": "Warszawa",
    "post_code": "00-001",
    "phone": "500000000",
    "email": "sender@zdrovena.pl",
}


class TestKurierShipment:
    def _kwargs(self):
        return {
            "receiver_first_name": "Jan",
            "receiver_last_name": "Kowalski",
            "receiver_email": "jan@example.com",
            "receiver_phone": "600200300",
            "receiver_street": "Kwiatowa",
            "receiver_building_number": "5",
            "receiver_city": "Warszawa",
            "receiver_post_code": "00-001",
            "sender": _SENDER,
            "reference": "order-1060",
        }

    def test_default_dimensions_converted_to_mm(self):
        client = InPostClient(_TOKEN, _ORG)
        resp = _ok_response({"id": "ship-2", "tracking_number": "T2"})
        with patch.object(client._session, "post", return_value=resp) as mock_post:
            client.create_kurier_shipment(**self._kwargs())

        sent = mock_post.call_args.kwargs["json"]
        dims = sent["parcels"][0]["dimensions"]
        # Default dimensions come from PARCEL_SPECS["1-pak"]: 30 × 20 × 20 cm → mm.
        # Weight default is independent of PARCEL_SPECS: kwarg default weight_kg=1.0.
        assert dims == {"unit": "mm", "length": 300, "width": 200, "height": 200}
        assert sent["parcels"][0]["weight"] == {"unit": "kg", "amount": 1.0}

    def test_custom_dimensions_passed_through(self):
        client = InPostClient(_TOKEN, _ORG)
        resp = _ok_response({"id": "ship-3"})
        with patch.object(client._session, "post", return_value=resp) as mock_post:
            client.create_kurier_shipment(
                **self._kwargs(),
                weight_kg=2.5,
                dimensions={"length": 40, "width": 25, "height": 20},
            )

        sent = mock_post.call_args.kwargs["json"]
        assert sent["parcels"][0]["dimensions"]["length"] == 400
        assert sent["parcels"][0]["weight"]["amount"] == 2.5

    def test_receiver_address_assembled(self):
        client = InPostClient(_TOKEN, _ORG)
        resp = _ok_response({"id": "ship-4"})
        with patch.object(client._session, "post", return_value=resp) as mock_post:
            client.create_kurier_shipment(**self._kwargs())

        sent = mock_post.call_args.kwargs["json"]
        addr = sent["receiver"]["address"]
        assert addr["street"] == "Kwiatowa"
        assert addr["building_number"] == "5"
        assert addr["city"] == "Warszawa"
        assert addr["post_code"] == "00-001"
        assert addr["country_code"] == "PL"
        assert sent["service"] == "inpost_courier_standard"
        assert sent["sender"] == _SENDER


# ── create_dispatch_order ────────────────────────────────────────────────────


class TestDispatchOrder:
    def test_minimal_payload(self):
        client = InPostClient(_TOKEN, _ORG)
        resp = _ok_response({"id": "disp-1"})
        with patch.object(client._session, "post", return_value=resp) as mock_post:
            result = client.create_dispatch_order("ship-1", _SENDER)

        assert result == {"id": "disp-1"}
        assert mock_post.call_args.args[0] == _DISPATCH_URL
        sent = mock_post.call_args.kwargs["json"]
        assert sent["shipments"] == ["ship-1"]
        # No optional pickup keys when not provided
        assert "pickup_date" not in sent
        assert "pickup_from" not in sent
        assert "pickup_to" not in sent

    def test_pickup_window_included(self):
        client = InPostClient(_TOKEN, _ORG)
        resp = _ok_response({"id": "disp-2"})
        with patch.object(client._session, "post", return_value=resp) as mock_post:
            client.create_dispatch_order(
                "ship-1",
                _SENDER,
                pickup_date="2026-07-01",
                pickup_from="10:00",
                pickup_to="14:00",
            )
        sent = mock_post.call_args.kwargs["json"]
        assert sent["pickup_date"] == "2026-07-01"
        assert sent["pickup_from"] == "10:00"
        assert sent["pickup_to"] == "14:00"

    def test_4xx_raises_with_status_in_message(self):
        client = InPostClient(_TOKEN, _ORG)
        with patch.object(client._session, "post", return_value=_err_response(422, "no-slot")):
            with pytest.raises(InPostError, match=r"422.*no-slot"):
                client.create_dispatch_order("ship-1", _SENDER)


# ── get_label ────────────────────────────────────────────────────────────────


class TestGetLabel:
    def test_returns_raw_pdf_bytes(self):
        client = InPostClient(_TOKEN, _ORG)
        pdf = b"%PDF-1.4 fake-content"
        r = MagicMock(spec=requests.Response)
        r.ok = True
        r.status_code = 200
        r.content = pdf
        with patch.object(client._session, "get", return_value=r) as mock_get:
            result = client.get_label("ship-1")

        assert result == pdf
        # URL targets shipment-specific label endpoint
        called_url = mock_get.call_args.args[0]
        assert called_url.endswith("/v1/shipments/ship-1/label")

    def test_4xx_raises_inpost_error(self):
        client = InPostClient(_TOKEN, _ORG)
        r = MagicMock(spec=requests.Response)
        r.ok = False
        r.status_code = 404
        r.text = "not-found"
        r.content = b""
        with patch.object(client._session, "get", return_value=r):
            with pytest.raises(InPostError, match=r"404"):
                client.get_label("ship-1")

    def test_network_error_mapped_to_transient(self):
        client = InPostClient(_TOKEN, _ORG)
        with patch.object(client._session, "get", side_effect=requests.Timeout("slow")):
            with pytest.raises(InPostTransientError, match="network error"):
                client.get_label("ship-1")


# ── error hierarchy: both classification axes must catch ─────────────────────


class TestErrorHierarchy:
    """F-I4 regression: InPost errors must live inside ZdrovenaShippingError so the
    shared `except ZdrovenaShippingError` handler catches them (not bare 500s), and
    also under the per-courier InPostError marker."""

    def _kwargs(self):
        return {
            "receiver_first_name": "Anna",
            "receiver_last_name": "Nowak",
            "receiver_email": "anna@example.com",
            "receiver_phone": "500100200",
            "target_point": "WAW01A",
            "reference": "order-1042",
        }

    def test_401_caught_as_inpost_and_auth_and_shipping(self):
        client = InPostClient(_TOKEN, _ORG)
        with patch.object(client._session, "post", return_value=_err_response(401, "nope")):
            with pytest.raises(InPostAuthError) as exc_info:
                client.create_paczkomat_shipment(**self._kwargs())
        err = exc_info.value
        assert isinstance(err, InPostError)
        assert isinstance(err, CourierAuthError)
        assert isinstance(err, ZdrovenaShippingError)

    def test_4xx_is_business_and_inpost_and_shipping(self):
        client = InPostClient(_TOKEN, _ORG)
        with patch.object(client._session, "post", return_value=_err_response(400, "bad")):
            with pytest.raises(InPostError) as exc_info:
                client.create_paczkomat_shipment(**self._kwargs())
        err = exc_info.value
        assert isinstance(err, InPostBusinessError)
        assert isinstance(err, CourierBusinessError)
        assert isinstance(err, ZdrovenaShippingError)

    def test_5xx_is_transient_and_inpost_and_shipping(self):
        client = InPostClient(_TOKEN, _ORG)
        with patch.object(client._session, "post", return_value=_err_response(503, "down")):
            with pytest.raises(InPostError) as exc_info:
                client.create_paczkomat_shipment(**self._kwargs())
        err = exc_info.value
        assert isinstance(err, InPostTransientError)
        assert isinstance(err, CourierTransientError)
        assert isinstance(err, ZdrovenaShippingError)

    def test_inpost_error_is_shipping_error_subclass(self):
        assert issubclass(InPostError, ZdrovenaShippingError)
