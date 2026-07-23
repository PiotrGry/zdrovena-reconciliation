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

    def test_base_url_has_no_embedded_credentials(self):
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

    def test_explicit_parcels_replace_template_fallback(self):
        client = InPostClient(_TOKEN, _ORG)
        parcels = [{"template": "medium"}, {"template": "large"}]
        with patch.object(
            client._session,
            "post",
            return_value=_ok_response({"id": "p-multi"}),
        ) as mock_post:
            client.create_paczkomat_shipment(**self._kwargs(), parcels=parcels)

        assert mock_post.call_args.kwargs["json"]["parcels"] == parcels

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

    def test_explicit_parcels_replace_single_parcel_fallback(self):
        client = InPostClient(_TOKEN, _ORG)
        parcels = [
            {
                "dimensions": {
                    "unit": "mm",
                    "length": 300,
                    "width": 200,
                    "height": 200,
                },
                "weight": {"unit": "kg", "amount": 6.0},
            },
            {
                "dimensions": {
                    "unit": "mm",
                    "length": 200,
                    "width": 150,
                    "height": 200,
                },
                "weight": {"unit": "kg", "amount": 3.0},
            },
        ]
        with patch.object(
            client._session,
            "post",
            return_value=_ok_response({"id": "ship-multi"}),
        ) as mock_post:
            client.create_kurier_shipment(**self._kwargs(), parcels=parcels)

        assert mock_post.call_args.kwargs["json"]["parcels"] == parcels

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

    def test_courier_service_env_var_override(self, monkeypatch):
        import zdrovena.common.inpost as inpost_mod

        monkeypatch.setattr(inpost_mod, "_COURIER_SERVICE", "inpost_courier_c2c")
        client = InPostClient(_TOKEN, _ORG)
        resp = _ok_response({"id": "ship-env"})
        with patch.object(client._session, "post", return_value=resp) as mock_post:
            client.create_kurier_shipment(**self._kwargs())
        sent = mock_post.call_args.kwargs["json"]
        assert sent["service"] == "inpost_courier_c2c"


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


# ── cancel_shipment ──────────────────────────────────────────────────────────


def _ok_get_status(status_value: str = "confirmed") -> MagicMock:
    """Build a mocked GET response for the cancel_shipment pre-flight."""
    r = MagicMock(spec=requests.Response)
    r.ok = True
    r.status_code = 200
    r.json.return_value = {"status": status_value}
    return r


class TestCancelShipment:
    def test_success_sends_delete_to_shipment_url(self):
        client = InPostClient(_TOKEN, _ORG)
        get_resp = _ok_get_status("confirmed")
        delete_resp = MagicMock(spec=requests.Response)
        delete_resp.ok = True
        delete_resp.status_code = 204
        with patch.object(client._session, "get", return_value=get_resp):
            with patch.object(client._session, "delete", return_value=delete_resp) as mock_delete:
                result = client.cancel_shipment("ship-42")
        url = mock_delete.call_args.args[0]
        assert url.endswith("/v1/shipments/ship-42")
        assert result is None

    def test_422_from_server_raises_not_cancellable(self):
        """P1-4: server-side 422 on DELETE surfaces InPostShipmentNotCancellable."""
        from zdrovena.common.shipping_exceptions import InPostShipmentNotCancellable

        client = InPostClient(_TOKEN, _ORG)
        get_resp = _ok_get_status("confirmed")
        del_resp = MagicMock(spec=requests.Response)
        del_resp.ok = False
        del_resp.status_code = 422
        del_resp.text = '{"error":"already_dispatched","message":"..."}'
        del_resp.json.return_value = {"error": "already_dispatched"}
        with patch.object(client._session, "get", return_value=get_resp):
            with patch.object(client._session, "delete", return_value=del_resp):
                with pytest.raises(InPostShipmentNotCancellable):
                    client.cancel_shipment("ship-42")

    def test_other_4xx_raises_with_status(self):
        client = InPostClient(_TOKEN, _ORG)
        get_resp = _ok_get_status("confirmed")
        del_resp = MagicMock(spec=requests.Response)
        del_resp.ok = False
        del_resp.status_code = 404
        del_resp.text = "not found"
        del_resp.json.side_effect = ValueError
        with patch.object(client._session, "get", return_value=get_resp):
            with patch.object(client._session, "delete", return_value=del_resp):
                with pytest.raises(InPostError, match="404"):
                    client.cancel_shipment("ship-42")

    def test_preflight_blocks_dispatched_status(self):
        """P1-4: pre-flight GET status detects dispatched -> raises without DELETE."""
        from zdrovena.common.shipping_exceptions import InPostShipmentNotCancellable

        client = InPostClient(_TOKEN, _ORG)
        get_resp = _ok_get_status("dispatched_by_sender")
        with patch.object(client._session, "get", return_value=get_resp):
            with patch.object(client._session, "delete") as mock_delete:
                with pytest.raises(InPostShipmentNotCancellable) as exc:
                    client.cancel_shipment("ship-42")
        mock_delete.assert_not_called()  # DELETE never attempted
        assert exc.value.current_status == "dispatched_by_sender"
        assert exc.value.shipment_id == "ship-42"

    def test_preflight_blocks_delivered_status(self):
        from zdrovena.common.shipping_exceptions import InPostShipmentNotCancellable

        client = InPostClient(_TOKEN, _ORG)
        get_resp = _ok_get_status("delivered")
        with patch.object(client._session, "get", return_value=get_resp):
            with patch.object(client._session, "delete") as mock_delete:
                with pytest.raises(InPostShipmentNotCancellable):
                    client.cancel_shipment("ship-42")
        mock_delete.assert_not_called()

    def test_preflight_404_falls_through_to_delete(self):
        """If GET fails with 4xx (e.g. shipment gone), still attempt DELETE."""
        client = InPostClient(_TOKEN, _ORG)
        get_resp = MagicMock(spec=requests.Response)
        get_resp.ok = False
        get_resp.status_code = 404
        get_resp.text = "not found"
        get_resp.json.side_effect = ValueError
        del_resp = MagicMock(spec=requests.Response)
        del_resp.ok = True
        del_resp.status_code = 204
        with patch.object(client._session, "get", return_value=get_resp):
            with patch.object(client._session, "delete", return_value=del_resp) as mock_delete:
                # Any 4xx on GET raises InPostBusinessError inside cancel_shipment,
                # which the method catches and falls through to DELETE.
                client.cancel_shipment("ship-42")
        mock_delete.assert_called_once()


class TestOrganizationErrorSurfacing:
    """P1-5: debt_collection / trucker_id_not_set surface as InPostOrganizationError."""

    def test_debt_collection_raises_organization_error(self):
        from zdrovena.common.shipping_exceptions import InPostOrganizationError

        client = InPostClient(_TOKEN, _ORG)
        r = MagicMock(spec=requests.Response)
        r.ok = False
        r.status_code = 400
        r.text = '{"error":"debt_collection","message":"Account is on billing hold"}'
        r.json.return_value = {
            "error": "debt_collection",
            "message": "Account is on billing hold",
        }
        with patch.object(client._session, "post", return_value=r):
            with pytest.raises(InPostOrganizationError) as exc:
                client.create_paczkomat_shipment(
                    receiver_first_name="Jan",
                    receiver_last_name="Kowalski",
                    receiver_email="jk@example.com",
                    receiver_phone="600000000",
                    target_point="WAW01A",
                    reference="ORD-1",
                )
        assert exc.value.code == "debt_collection"

    def test_trucker_id_not_set_raises_organization_error(self):
        from zdrovena.common.shipping_exceptions import InPostOrganizationError

        client = InPostClient(_TOKEN, _ORG)
        r = MagicMock(spec=requests.Response)
        r.ok = False
        r.status_code = 400
        r.text = '{"error":"trucker_id_not_set"}'
        r.json.return_value = {"error": "trucker_id_not_set"}
        with patch.object(client._session, "post", return_value=r):
            with pytest.raises(InPostOrganizationError) as exc:
                client.create_paczkomat_shipment(
                    receiver_first_name="Jan",
                    receiver_last_name="Kowalski",
                    receiver_email="jk@example.com",
                    receiver_phone="600000000",
                    target_point="WAW01A",
                    reference="ORD-1",
                )
        assert exc.value.code == "trucker_id_not_set"

    def test_unknown_error_code_still_business_error(self):
        """Non-org 4xx codes stay as plain InPostBusinessError."""
        client = InPostClient(_TOKEN, _ORG)
        r = MagicMock(spec=requests.Response)
        r.ok = False
        r.status_code = 400
        r.text = '{"error":"validation_failed"}'
        r.json.return_value = {"error": "validation_failed"}
        with patch.object(client._session, "post", return_value=r):
            from zdrovena.common.shipping_exceptions import (
                InPostBusinessError,
                InPostOrganizationError,
            )

            with pytest.raises(InPostBusinessError) as exc:
                client.create_paczkomat_shipment(
                    receiver_first_name="Jan",
                    receiver_last_name="Kowalski",
                    receiver_email="jk@example.com",
                    receiver_phone="600000000",
                    target_point="WAW01A",
                    reference="ORD-1",
                )
            # Must NOT be the organisation subclass
            assert not isinstance(exc.value, InPostOrganizationError)
