"""Tests for zdrovena.common.apaczka.ApaczkaClient.

Apaczka uses a per-request HMAC-SHA256 signature scheme implemented in-house
(no SDK). Misordered fields or wrong JSON separators break authentication
without a clear error. Pinning the signature to a known vector guards against
silent regressions.

Audit reference: zdrovena_test_audit.md §7.3 — Apaczka has zero unit tests
despite owning the most complex code path.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
import requests

from zdrovena.common.apaczka import (
    _SERVICE_CACHE_KEY,
    ApaczkaClient,
    ApaczkaError,
    _sign,
)
from zdrovena.common.shipping_exceptions import (
    ApaczkaAuthError,
    ApaczkaBusinessError,
    ApaczkaInsufficientBalanceError,
    ApaczkaSignatureError,
    ApaczkaTransientError,
    CourierAuthError,
    CourierBusinessError,
    CourierTransientError,
    ZdrovenaShippingError,
)

_APP_ID = "app1"
_SECRET = "sec1"
_SERVICE_ID = "svc-99"
_BASE_URL = "https://www.apaczka.pl/api/v2"


def _ok_response(json_payload: dict, status: int = 200) -> MagicMock:
    r = MagicMock(spec=requests.Response)
    r.ok = True
    r.status_code = status
    r.json.return_value = json_payload
    r.text = ""
    return r


def _err_response(status: int, text: str = "boom") -> MagicMock:
    r = MagicMock(spec=requests.Response)
    r.ok = False
    r.status_code = status
    r.json.return_value = {}
    r.text = text
    return r


# ── _sign — HMAC signature vector ─────────────────────────────────────────────


class TestSignVector:
    """Pin the HMAC implementation to a fixed input → expected hex digest.

    If this test breaks, either the signing rule changed (must coordinate
    with Apaczka API team) or a regression introduced subtle differences
    in JSON formatting / message layout.
    """

    def test_known_vector(self, monkeypatch):
        # Freeze time so expires is deterministic
        monkeypatch.setattr("zdrovena.common.apaczka.time.time", lambda: 1700000000)

        result = _sign(_APP_ID, _SECRET, "order_send", {"a": 1})

        # Computed manually (verified against live Apaczka API 2026-07):
        #   request_json = '{"a":1}'  (no spaces; separators=(",", ":"))
        #   expires = 1700000000 + 1800 = 1700001800
        #   route = "order_send/"  (trailing slash REQUIRED by Apaczka)
        #   msg = "app1:order_send/:{\"a\":1}:1700001800"
        #   hmac.new(b"sec1", msg.encode("utf-8"), hashlib.sha256).hexdigest()
        assert result["app_id"] == _APP_ID
        assert result["expires"] == "1700001800"
        assert result["request"] == '{"a":1}'
        assert (
            result["signature"]
            == "df4b76c9797a5a59c1e49b760203a4ddb79e5dcb601026d12a0722b23d879664"
        )

    def test_route_gets_trailing_slash(self, monkeypatch):
        """Regression: signature must include the trailing slash on the endpoint.

        Verified 2026-07 against live Apaczka API — signing bare
        \"service_structure\" returns 'Signature doesn't match', while
        \"service_structure/\" is accepted.
        """
        import hashlib as _hashlib
        import hmac as _hmac

        monkeypatch.setattr("zdrovena.common.apaczka.time.time", lambda: 1700000000)
        # Endpoint passed WITHOUT trailing slash — _sign must add it.
        result = _sign(_APP_ID, _SECRET, "service_structure", {})

        expected_msg = f"{_APP_ID}:service_structure/:{{}}:1700001800"
        expected_sig = _hmac.new(
            _SECRET.encode(), expected_msg.encode("utf-8"), _hashlib.sha256
        ).hexdigest()
        assert result["signature"] == expected_sig

    def test_route_slash_idempotent(self, monkeypatch):
        """Passing endpoint already ending in / must not produce a double slash."""
        monkeypatch.setattr("zdrovena.common.apaczka.time.time", lambda: 1700000000)
        a = _sign(_APP_ID, _SECRET, "order_send", {"a": 1})
        b = _sign(_APP_ID, _SECRET, "order_send/", {"a": 1})
        assert a["signature"] == b["signature"]

    def test_request_json_uses_compact_separators(self, monkeypatch):
        """Apaczka spec requires JSON with no spaces between separators."""
        monkeypatch.setattr("zdrovena.common.apaczka.time.time", lambda: 1700000000)
        result = _sign(_APP_ID, _SECRET, "ep", {"x": 1, "y": "z"})
        assert ", " not in result["request"]
        assert ": " not in result["request"]

    def test_expires_is_30_minutes_in_future(self, monkeypatch):
        monkeypatch.setattr("zdrovena.common.apaczka.time.time", lambda: 1_000_000_000)
        result = _sign(_APP_ID, _SECRET, "ep", {})
        assert int(result["expires"]) - 1_000_000_000 == 1800

    def test_different_data_produces_different_signature(self, monkeypatch):
        monkeypatch.setattr("zdrovena.common.apaczka.time.time", lambda: 1700000000)
        s1 = _sign(_APP_ID, _SECRET, "ep", {"a": 1})
        s2 = _sign(_APP_ID, _SECRET, "ep", {"a": 2})
        assert s1["signature"] != s2["signature"]

    def test_different_secret_produces_different_signature(self, monkeypatch):
        monkeypatch.setattr("zdrovena.common.apaczka.time.time", lambda: 1700000000)
        s1 = _sign(_APP_ID, "secretA", "ep", {})
        s2 = _sign(_APP_ID, "secretB", "ep", {})
        assert s1["signature"] != s2["signature"]

    def test_polish_characters_not_escaped(self, monkeypatch):
        """Regression: json.dumps must use ensure_ascii=False.

        Apaczka's PHP server uses json_encode with JSON_UNESCAPED_UNICODE.
        If we sign the ASCII-escaped variant (\\u0142) but send the raw UTF-8
        bytes (ł), the server re-computes a different signature and rejects
        the request with \"Signature doesn't match\".
        """
        monkeypatch.setattr("zdrovena.common.apaczka.time.time", lambda: 1700000000)
        result = _sign(_APP_ID, _SECRET, "order_send", {"city": "Kraków", "name": "Piotr Gryzło"})

        # Must contain raw UTF-8 Polish chars, NOT \uXXXX escapes.
        assert "Kraków" in result["request"]
        assert "Gryzło" in result["request"]
        assert "\\u" not in result["request"]

    def test_polish_signature_matches_utf8_bytes(self, monkeypatch):
        """Signature is computed over UTF-8 bytes of the unescaped JSON."""
        import hashlib as _hashlib
        import hmac as _hmac

        monkeypatch.setattr("zdrovena.common.apaczka.time.time", lambda: 1700000000)
        data = {"city": "Kraków"}
        result = _sign(_APP_ID, _SECRET, "order_send", data)

        expected_json = '{"city":"Kraków"}'  # unescaped
        expected_msg = f"{_APP_ID}:order_send/:{expected_json}:1700001800"
        expected_sig = _hmac.new(
            _SECRET.encode(), expected_msg.encode("utf-8"), _hashlib.sha256
        ).hexdigest()

        assert result["request"] == expected_json
        assert result["signature"] == expected_sig


# ── _call — error mapping ─────────────────────────────────────────────────────


class TestApaczkaCallErrorMapping:
    def test_http_4xx_raises_with_status(self):
        client = ApaczkaClient(_APP_ID, _SECRET, _SERVICE_ID, storage=MagicMock())
        with patch.object(client._session, "post", return_value=_err_response(403, "denied")):
            with pytest.raises(ApaczkaError, match=r"403.*denied"):
                client._call("order_send", {})

    def test_http_5xx_raises(self):
        client = ApaczkaClient(_APP_ID, _SECRET, _SERVICE_ID, storage=MagicMock())
        with patch.object(client._session, "post", return_value=_err_response(500, "oops")):
            with pytest.raises(ApaczkaError, match=r"500"):
                client._call("order_send", {})

    def test_200_with_error_status_field_raises(self):
        """Apaczka returns business errors as HTTP 200 with status!=200 in the JSON body."""
        client = ApaczkaClient(_APP_ID, _SECRET, _SERVICE_ID, storage=MagicMock())
        body = {"status": 400, "message": "Invalid service_id"}
        with patch.object(client._session, "post", return_value=_ok_response(body)):
            with pytest.raises(ApaczkaError, match="Invalid service_id"):
                client._call("order_send", {})

    def test_200_with_status_200_returns_result(self):
        client = ApaczkaClient(_APP_ID, _SECRET, _SERVICE_ID, storage=MagicMock())
        body = {"status": 200, "response": {"id": 42}}
        with patch.object(client._session, "post", return_value=_ok_response(body)):
            result = client._call("order_send", {"a": 1})
        assert result == body

    def test_call_posts_signed_payload_as_form(self, monkeypatch):
        monkeypatch.setattr("zdrovena.common.apaczka.time.time", lambda: 1700000000)
        client = ApaczkaClient(_APP_ID, _SECRET, _SERVICE_ID, storage=MagicMock())
        body = {"status": 200, "response": {}}
        with patch.object(client._session, "post", return_value=_ok_response(body)) as mock_post:
            client._call("order_send", {"a": 1})

        # POSTed as form-data (not JSON)
        assert "json" not in mock_post.call_args.kwargs
        sent = mock_post.call_args.kwargs["data"]
        assert sent["app_id"] == _APP_ID
        assert sent["request"] == '{"a":1}'
        assert sent["expires"] == "1700001800"
        assert sent["signature"]  # non-empty hex
        # URL includes trailing slash + endpoint
        assert mock_post.call_args.args[0] == f"{_BASE_URL}/order_send/"
        # Timeout was specified
        assert mock_post.call_args.kwargs["timeout"] > 0


# ── Service-structure cache ──────────────────────────────────────────────────


class _FakeStorageWithCache:
    """Storage stub matching the {stream, upload_stream} pair used by ApaczkaClient.

    Real ApaczkaClient calls ``storage.stream(key)`` (returns iterator of bytes
    chunks) for reads and ``storage.upload_stream(buf, key, content_type)`` for
    writes. See zdrovena/common/apaczka.py::_get_service_structure.
    """

    def __init__(self, payload: bytes | None = None) -> None:
        self._payload = payload
        self.uploaded: list[tuple[str, bytes]] = []

    def stream(self, key: str, chunk_size: int = 4 * 1024 * 1024):
        if self._payload is None:
            raise FileNotFoundError(key)
        yield self._payload

    def upload_stream(self, buf, key: str, content_type: str = ""):
        data = buf.getvalue() if hasattr(buf, "getvalue") else b""
        self.uploaded.append((key, data))


class TestServiceStructureCache:
    def test_returns_cached_when_fresh(self):
        fresh_iso = datetime.now(timezone.utc).isoformat()
        cached = {"fetched_at": fresh_iso, "services": [{"id": "s1"}]}
        storage = _FakeStorageWithCache(payload=json.dumps(cached).encode())
        client = ApaczkaClient(_APP_ID, _SECRET, _SERVICE_ID, storage=storage)

        with patch.object(client._session, "post") as mock_post:
            services = client._get_service_structure()

        assert services == [{"id": "s1"}]
        # No HTTP call — served from cache
        mock_post.assert_not_called()

    def test_refetches_when_cache_expired(self, monkeypatch):
        old_iso = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        cached = {"fetched_at": old_iso, "services": [{"id": "stale"}]}
        storage = _FakeStorageWithCache(payload=json.dumps(cached).encode())
        client = ApaczkaClient(_APP_ID, _SECRET, _SERVICE_ID, storage=storage)

        api_response = _ok_response({"status": 200, "response": {"services": [{"id": "fresh"}]}})
        with patch.object(client._session, "post", return_value=api_response):
            services = client._get_service_structure()

        assert services == [{"id": "fresh"}]
        # Cache was refreshed in storage
        assert len(storage.uploaded) == 1
        assert storage.uploaded[0][0] == _SERVICE_CACHE_KEY

    def test_refetches_when_no_cache_exists(self):
        storage = _FakeStorageWithCache(payload=None)  # download raises
        client = ApaczkaClient(_APP_ID, _SECRET, _SERVICE_ID, storage=storage)

        api_response = _ok_response(
            {"status": 200, "response": {"services": [{"id": "a"}, {"id": "b"}]}}
        )
        with patch.object(client._session, "post", return_value=api_response):
            services = client._get_service_structure()

        assert services == [{"id": "a"}, {"id": "b"}]


# ── create_shipment ──────────────────────────────────────────────────────────


_SENDER = {
    "name": "Zdrovena",
    "firstname": "",
    "lastname": "Zdrovena",
    "email": "sender@zdrovena.pl",
    "phone": "500000000",
    "street": "Testowa 1",
    "city": "Warszawa",
    "post_code": "00-001",
}


class TestCreateShipment:
    def _kwargs(self):
        return {
            "receiver_name": "Jan Kowalski",
            "receiver_firstname": "Jan",
            "receiver_lastname": "Kowalski",
            "receiver_email": "jan@example.com",
            "receiver_phone": "600200300",
            "receiver_address": "Kwiatowa 1",
            "receiver_city": "Kraków",
            "receiver_zip": "30-001",
            "sender": _SENDER,
            "reference": "order-2042",
        }

    def test_success_returns_response_dict(self):
        client = ApaczkaClient(_APP_ID, _SECRET, _SERVICE_ID, storage=MagicMock())
        api_response = _ok_response(
            {
                "status": 200,
                "response": {"order": {"id": "ap-1", "waybill_number": "WAY001"}},
            }
        )
        with patch.object(client._session, "post", return_value=api_response) as mock_post:
            result = client.create_shipment(**self._kwargs())

        assert result == {"id": "ap-1", "waybill_number": "WAY001"}
        # URL targets order_send endpoint
        assert mock_post.call_args.args[0] == f"{_BASE_URL}/order_send/"

    def test_payload_includes_sender_and_receiver(self):
        client = ApaczkaClient(_APP_ID, _SECRET, _SERVICE_ID, storage=MagicMock())
        api_response = _ok_response({"status": 200, "response": {"id": "ap-2"}})
        with patch.object(client._session, "post", return_value=api_response) as mock_post:
            client.create_shipment(**self._kwargs())

        # Signed body is sent as form-data — recover signed JSON
        sent_form = mock_post.call_args.kwargs["data"]
        data = json.loads(sent_form["request"])

        order = data["order"]
        assert order["service_id"] == _SERVICE_ID
        assert order["externalId"] == "order-2042"
        assert order["address"]["sender"]["email"] == "sender@zdrovena.pl"
        assert order["address"]["receiver"]["line1"] == "Kwiatowa 1"
        assert order["address"]["receiver"]["postal_code"] == "30-001"
        assert order["address"]["receiver"]["country_code"] == "PL"
        assert order["shipment"][0]["shipment_type_code"] == "PACZKA"

    def test_explicit_shipments_replace_single_parcel_fallback(self):
        client = ApaczkaClient(_APP_ID, _SECRET, _SERVICE_ID, storage=MagicMock())
        shipments = [
            {
                "weight": 6.0,
                "dimension1": 30.0,
                "dimension2": 20.0,
                "dimension3": 20.0,
                "is_nstd": 0,
                "shipment_type_code": "PACZKA",
            },
            {
                "weight": 3.0,
                "dimension1": 20.0,
                "dimension2": 15.0,
                "dimension3": 20.0,
                "is_nstd": 0,
                "shipment_type_code": "PACZKA",
            },
        ]
        api_response = _ok_response({"status": 200, "response": {"id": "ap-multi"}})
        with patch.object(
            client._session,
            "post",
            return_value=api_response,
        ) as mock_post:
            client.create_shipment(**self._kwargs(), shipments=shipments)

        sent_form = mock_post.call_args.kwargs["data"]
        data = json.loads(sent_form["request"])
        assert data["order"]["shipment"] == shipments

    def test_pickup_point_id_sent_as_foreign_address_id(self):
        client = ApaczkaClient(_APP_ID, _SECRET, "23", storage=MagicMock())
        api_response = _ok_response({"status": 200, "response": {"id": "ap-point"}})
        with patch.object(client._session, "post", return_value=api_response) as mock_post:
            client.create_shipment(**self._kwargs(), receiver_point_id="PL55338")

        sent_form = mock_post.call_args.kwargs["data"]
        data = json.loads(sent_form["request"])
        assert data["order"]["address"]["receiver"]["foreign_address_id"] == "PL55338"

    def test_sender_building_number_included_in_address(self):
        """Regression: _get_sender() stores building_number separately; create_shipment
        must join it with street so Apaczka doesn't receive a bare street name."""
        sender_with_bnum = {
            **_SENDER,
            "street": "Testowa",
            "building_number": "7",
        }
        client = ApaczkaClient(_APP_ID, _SECRET, _SERVICE_ID, storage=MagicMock())
        api_response = _ok_response({"status": 200, "response": {"id": "ap-bnum"}})
        with patch.object(client._session, "post", return_value=api_response) as mock_post:
            client.create_shipment(**{**self._kwargs(), "sender": sender_with_bnum})

        sent_form = mock_post.call_args.kwargs["data"]
        data = json.loads(sent_form["request"])
        assert data["order"]["address"]["sender"]["line1"] == "Testowa 7"

    def test_pickup_window_included_when_provided(self):
        client = ApaczkaClient(_APP_ID, _SECRET, _SERVICE_ID, storage=MagicMock())
        api_response = _ok_response({"status": 200, "response": {"id": "ap-3"}})
        with patch.object(client._session, "post", return_value=api_response) as mock_post:
            client.create_shipment(
                **self._kwargs(),
                pickup_date="2026-07-01",
                pickup_from="10:00",
                pickup_to="14:00",
            )
        sent_form = mock_post.call_args.kwargs["data"]
        data = json.loads(sent_form["request"])
        assert data["order"]["pickup"]["date"] == "2026-07-01"
        assert data["order"]["pickup"]["hours_from"] == "10:00"
        assert data["order"]["pickup"]["hours_to"] == "14:00"

    def test_no_pickup_window_when_omitted(self):
        client = ApaczkaClient(_APP_ID, _SECRET, _SERVICE_ID, storage=MagicMock())
        api_response = _ok_response({"status": 200, "response": {"id": "ap-4"}})
        with patch.object(client._session, "post", return_value=api_response) as mock_post:
            client.create_shipment(**self._kwargs())
        sent_form = mock_post.call_args.kwargs["data"]
        data = json.loads(sent_form["request"])
        assert data["order"]["pickup"] == {"type": "COURIER"}

    def test_business_error_raises_apaczka_error(self):
        client = ApaczkaClient(_APP_ID, _SECRET, _SERVICE_ID, storage=MagicMock())
        api_response = _ok_response({"status": 400, "message": "Invalid zip code"})
        with patch.object(client._session, "post", return_value=api_response):
            with pytest.raises(ApaczkaError, match="Invalid zip"):
                client.create_shipment(**self._kwargs())

    def test_http_5xx_raises_apaczka_error(self):
        client = ApaczkaClient(_APP_ID, _SECRET, _SERVICE_ID, storage=MagicMock())
        with patch.object(client._session, "post", return_value=_err_response(502, "bad gw")):
            with pytest.raises(ApaczkaError, match=r"502"):
                client.create_shipment(**self._kwargs())


# ── get_label ────────────────────────────────────────────────────────────────


class TestGetLabel:
    def test_decodes_base64_waybill(self):
        import base64

        pdf = b"%PDF-1.4 fake-label"
        encoded = base64.b64encode(pdf).decode()
        client = ApaczkaClient(_APP_ID, _SECRET, _SERVICE_ID, storage=MagicMock())
        api_response = _ok_response({"status": 200, "response": {"waybill": encoded}})
        with patch.object(client._session, "post", return_value=api_response):
            result = client.get_label("ord-1")
        assert result == pdf

    def test_missing_waybill_raises(self):
        client = ApaczkaClient(_APP_ID, _SECRET, _SERVICE_ID, storage=MagicMock())
        api_response = _ok_response({"status": 200, "response": {}})
        with patch.object(client._session, "post", return_value=api_response):
            with pytest.raises(ApaczkaError, match="No waybill"):
                client.get_label("ord-1")

    def test_business_error_propagates(self):
        client = ApaczkaClient(_APP_ID, _SECRET, _SERVICE_ID, storage=MagicMock())
        api_response = _ok_response({"status": 404, "message": "order not found"})
        with patch.object(client._session, "post", return_value=api_response):
            with pytest.raises(ApaczkaError):
                client.get_label("missing")


# ── error hierarchy: both classification axes must catch ─────────────────────


class TestErrorHierarchy:
    """F-A4 regression: Apaczka errors must live inside ZdrovenaShippingError so the
    shared `except ZdrovenaShippingError` handler catches them (not bare 500s), and
    also under the per-courier ApaczkaError marker + the handling-semantics axis."""

    def test_403_is_auth_and_apaczka_and_shipping(self):
        client = ApaczkaClient(_APP_ID, _SECRET, _SERVICE_ID, storage=MagicMock())
        with patch.object(client._session, "post", return_value=_err_response(403, "denied")):
            with pytest.raises(ApaczkaAuthError) as exc_info:
                client._call("order_send", {})
        err = exc_info.value
        assert isinstance(err, ApaczkaError)
        assert isinstance(err, CourierAuthError)
        assert isinstance(err, ZdrovenaShippingError)

    def test_4xx_is_business_and_apaczka_and_shipping(self):
        client = ApaczkaClient(_APP_ID, _SECRET, _SERVICE_ID, storage=MagicMock())
        with patch.object(client._session, "post", return_value=_err_response(404, "nope")):
            with pytest.raises(ApaczkaError) as exc_info:
                client._call("order_send", {})
        err = exc_info.value
        assert isinstance(err, ApaczkaBusinessError)
        assert isinstance(err, CourierBusinessError)
        assert isinstance(err, ZdrovenaShippingError)

    def test_5xx_is_transient_and_apaczka_and_shipping(self):
        client = ApaczkaClient(_APP_ID, _SECRET, _SERVICE_ID, storage=MagicMock())
        with patch.object(client._session, "post", return_value=_err_response(500, "oops")):
            with pytest.raises(ApaczkaError) as exc_info:
                client._call("order_send", {})
        err = exc_info.value
        assert isinstance(err, ApaczkaTransientError)
        assert isinstance(err, CourierTransientError)
        assert isinstance(err, ZdrovenaShippingError)

    def test_network_error_mapped_to_transient(self):
        client = ApaczkaClient(_APP_ID, _SECRET, _SERVICE_ID, storage=MagicMock())
        with patch.object(client._session, "post", side_effect=requests.ConnectionError("refused")):
            with pytest.raises(ApaczkaTransientError, match="network error"):
                client._call("order_send", {})

    def test_signature_rejection_in_body_is_auth(self):
        client = ApaczkaClient(_APP_ID, _SECRET, _SERVICE_ID, storage=MagicMock())
        body = {"status": 401, "message": "Invalid signature provided"}
        with patch.object(client._session, "post", return_value=_ok_response(body)):
            with pytest.raises(ApaczkaSignatureError) as exc_info:
                client._call("order_send", {})
        assert isinstance(exc_info.value, ApaczkaAuthError)

    def test_insufficient_balance_in_body_is_auth(self):
        client = ApaczkaClient(_APP_ID, _SECRET, _SERVICE_ID, storage=MagicMock())
        body = {"status": 402, "message": "Insufficient account balance"}
        with patch.object(client._session, "post", return_value=_ok_response(body)):
            with pytest.raises(ApaczkaInsufficientBalanceError) as exc_info:
                client._call("order_send", {})
        assert isinstance(exc_info.value, ApaczkaAuthError)

    def test_apaczka_error_is_shipping_error_subclass(self):
        assert issubclass(ApaczkaError, ZdrovenaShippingError)


# ── cancel_shipment ──────────────────────────────────────────────────────────


class TestCancelShipment:
    def test_posts_to_cancel_order_endpoint(self):
        client = ApaczkaClient(_APP_ID, _SECRET, _SERVICE_ID, storage=MagicMock())
        api_response = _ok_response({"status": 200, "response": {"cancelled": True}})
        with patch.object(client._session, "post", return_value=api_response) as mock_post:
            client.cancel_shipment("ord-99")

        url = mock_post.call_args.args[0]
        assert url.endswith("/cancel_order/ord-99/")

    def test_cancel_payload_is_empty(self):
        client = ApaczkaClient(_APP_ID, _SECRET, _SERVICE_ID, storage=MagicMock())
        api_response = _ok_response({"status": 200, "response": {}})
        with patch.object(client._session, "post", return_value=api_response) as mock_post:
            client.cancel_shipment("ord-99")

        form_data = mock_post.call_args.kwargs["data"]
        import json

        request_payload = json.loads(form_data["request"])
        assert request_payload == {}

    def test_business_error_propagates(self):
        client = ApaczkaClient(_APP_ID, _SECRET, _SERVICE_ID, storage=MagicMock())
        api_response = _ok_response({"status": 400, "message": "Already delivered"})
        with patch.object(client._session, "post", return_value=api_response):
            with pytest.raises(ApaczkaError, match="Already delivered"):
                client.cancel_shipment("ord-99")
