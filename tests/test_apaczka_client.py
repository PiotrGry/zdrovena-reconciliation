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

        # Computed manually:
        #   request_json = '{"a":1}'  (no spaces; separators=(",", ":"))
        #   expires = 1700000000 + 1800 = 1700001800
        #   msg = "app1:order_send:{\"a\":1}:1700001800"
        #   hmac.new(b"sec1", msg.encode(), hashlib.sha256).hexdigest()
        assert result["app_id"] == _APP_ID
        assert result["expires"] == "1700001800"
        assert result["request"] == '{"a":1}'
        assert (
            result["signature"]
            == "f13f5a7705a0042227b195a71fe5fb384ac4acba74f9dddbb9d05388d3827886"
        )

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

        api_response = _ok_response(
            {"status": 200, "response": {"services": [{"id": "fresh"}]}}
        )
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
            {"status": 200, "response": {"id": "ap-1", "waybill_number": "WAY001"}}
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

        assert data["service_id"] == _SERVICE_ID
        assert data["order_id"] == "order-2042"
        assert data["address"]["sender"]["email"] == "sender@zdrovena.pl"
        assert data["address"]["receiver"]["zip"] == "30-001"
        assert data["address"]["receiver"]["country_code"] == "PL"
        assert data["shipment"][0]["type"] == "package"

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
        assert data["options"]["pickup"]["date"] == "2026-07-01"
        assert data["options"]["pickup"]["hours_from"] == "10:00"
        assert data["options"]["pickup"]["hours_to"] == "14:00"

    def test_no_pickup_window_when_omitted(self):
        client = ApaczkaClient(_APP_ID, _SECRET, _SERVICE_ID, storage=MagicMock())
        api_response = _ok_response({"status": 200, "response": {"id": "ap-4"}})
        with patch.object(client._session, "post", return_value=api_response) as mock_post:
            client.create_shipment(**self._kwargs())
        sent_form = mock_post.call_args.kwargs["data"]
        data = json.loads(sent_form["request"])
        assert "pickup" not in data["options"]
        assert data["options"]["pickup_type"] == "courier"

    def test_business_error_raises_apaczka_error(self):
        client = ApaczkaClient(_APP_ID, _SECRET, _SERVICE_ID, storage=MagicMock())
        api_response = _ok_response(
            {"status": 400, "message": "Invalid zip code"}
        )
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
        api_response = _ok_response(
            {"status": 200, "response": {"waybill": encoded}}
        )
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
