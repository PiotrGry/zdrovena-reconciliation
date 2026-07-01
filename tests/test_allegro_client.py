"""Tests for zdrovena.common.allegro.AllegroClient.

Allegro REST API v1 client. Uses OAuth 2.0 refresh-token flow — access tokens
expire after 12h, refresh tokens after 3 months. All external calls are mocked
via `requests.Session.request` (no live network in tests).

Design contract:
- OAuth token cached in-memory with `expires_at` timestamp; refreshed lazily.
- 401/403 → AllegroAuthError. 4xx business → AllegroBusinessError.
  5xx/network → CourierServerError/CourierConnectionError/CourierTimeoutError.
- All API responses expected as JSON; endpoints demand
  `Accept: application/vnd.allegro.public.v1+json`.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest
import requests

from zdrovena.common.allegro import (
    _ACCEPT_HEADER,
    _AUTH_URL_PROD,
    _AUTH_URL_SANDBOX,
    _BASE_URL_PROD,
    _BASE_URL_SANDBOX,
    AllegroClient,
)
from zdrovena.common.shipping_exceptions import (
    AllegroAuthError,
    AllegroBusinessError,
    CourierConnectionError,
    CourierServerError,
    CourierTimeoutError,
)

_CLIENT_ID = "cid"
_CLIENT_SECRET = "csec"
_REFRESH_TOKEN = "rtok"


def _ok(json_payload, status: int = 200) -> MagicMock:
    r = MagicMock(spec=requests.Response)
    r.ok = True
    r.status_code = status
    r.json.return_value = json_payload
    r.text = ""
    r.content = b""
    return r


def _err(status: int, text: str = "boom", json_payload=None) -> MagicMock:
    r = MagicMock(spec=requests.Response)
    r.ok = False
    r.status_code = status
    r.json.return_value = json_payload or {}
    r.text = text
    r.content = text.encode()
    return r


def _make_client(env: str = "prod") -> AllegroClient:
    return AllegroClient(
        client_id=_CLIENT_ID,
        client_secret=_CLIENT_SECRET,
        refresh_token=_REFRESH_TOKEN,
        env=env,
    )


# ═══════════════════════════════════════════════════════════════════════════
# 1. Environment / base URL selection (2 tests)
# ═══════════════════════════════════════════════════════════════════════════


class TestEnvironment:
    def test_prod_env_uses_prod_urls(self):
        c = _make_client(env="prod")
        assert c._base_url == _BASE_URL_PROD
        assert c._auth_url == _AUTH_URL_PROD

    def test_sandbox_env_uses_sandbox_urls(self):
        c = _make_client(env="sandbox")
        assert c._base_url == _BASE_URL_SANDBOX
        assert c._auth_url == _AUTH_URL_SANDBOX


# ═══════════════════════════════════════════════════════════════════════════
# 2. OAuth token refresh (7 tests)
# ═══════════════════════════════════════════════════════════════════════════


class TestOAuth:
    def test_first_call_fetches_token(self, monkeypatch):
        c = _make_client()
        with patch.object(c._session, "request") as req:
            req.side_effect = [
                _ok({"access_token": "at1", "expires_in": 43200, "refresh_token": "rt2"}),
                _ok({"checkoutForms": [], "count": 0, "totalCount": 0}),
            ]
            c.list_orders()
        assert req.call_count == 2
        # First call must POST to auth URL
        first = req.call_args_list[0]
        assert first.args[0].upper() == "POST"
        assert first.args[1] == _AUTH_URL_PROD

    def test_uses_basic_auth_with_client_credentials(self):
        c = _make_client()
        with patch.object(c._session, "request") as req:
            req.side_effect = [
                _ok({"access_token": "at1", "expires_in": 43200}),
                _ok({"checkoutForms": [], "count": 0, "totalCount": 0}),
            ]
            c.list_orders()
        auth = req.call_args_list[0].kwargs.get("auth")
        assert auth is not None
        # requests.auth.HTTPBasicAuth-like tuple accepted too
        if isinstance(auth, tuple):
            assert auth == (_CLIENT_ID, _CLIENT_SECRET)
        else:
            assert auth.username == _CLIENT_ID
            assert auth.password == _CLIENT_SECRET

    def test_sends_refresh_token_grant(self):
        c = _make_client()
        with patch.object(c._session, "request") as req:
            req.side_effect = [
                _ok({"access_token": "at1", "expires_in": 43200}),
                _ok({"checkoutForms": [], "count": 0, "totalCount": 0}),
            ]
            c.list_orders()
        data = req.call_args_list[0].kwargs.get("data")
        assert data["grant_type"] == "refresh_token"
        assert data["refresh_token"] == _REFRESH_TOKEN

    def test_cached_token_reused_within_ttl(self, monkeypatch):
        c = _make_client()
        monkeypatch.setattr(time, "time", lambda: 1_000_000)
        with patch.object(c._session, "request") as req:
            req.side_effect = [
                _ok({"access_token": "at1", "expires_in": 43200}),
                _ok({"checkoutForms": [], "count": 0, "totalCount": 0}),
                _ok({"checkoutForms": [], "count": 0, "totalCount": 0}),
            ]
            c.list_orders()
            c.list_orders()
        # Only ONE auth call
        auth_calls = [call for call in req.call_args_list if call.args[1] == _AUTH_URL_PROD]
        assert len(auth_calls) == 1

    def test_expired_token_triggers_refresh(self, monkeypatch):
        c = _make_client()
        current = {"t": 1_000_000}
        monkeypatch.setattr(time, "time", lambda: current["t"])
        with patch.object(c._session, "request") as req:
            req.side_effect = [
                _ok({"access_token": "at1", "expires_in": 60}),
                _ok({"checkoutForms": [], "count": 0, "totalCount": 0}),
                _ok({"access_token": "at2", "expires_in": 60}),
                _ok({"checkoutForms": [], "count": 0, "totalCount": 0}),
            ]
            c.list_orders()
            current["t"] += 120  # past 60s expiry
            c.list_orders()
        auth_calls = [call for call in req.call_args_list if call.args[1] == _AUTH_URL_PROD]
        assert len(auth_calls) == 2

    def test_auth_401_raises_allegro_auth_error(self):
        c = _make_client()
        with patch.object(c._session, "request", return_value=_err(401, "invalid_grant")):
            with pytest.raises(AllegroAuthError):
                c.list_orders()

    def test_auth_403_raises_allegro_auth_error(self):
        c = _make_client()
        with patch.object(c._session, "request", return_value=_err(403, "forbidden")):
            with pytest.raises(AllegroAuthError):
                c.list_orders()


# ═══════════════════════════════════════════════════════════════════════════
# 3. HTTP layer / error mapping (6 tests)
# ═══════════════════════════════════════════════════════════════════════════


class TestHttpLayer:
    def _auth_ok(self):
        return _ok({"access_token": "at1", "expires_in": 43200})

    def test_authenticated_calls_include_bearer(self):
        c = _make_client()
        with patch.object(c._session, "request") as req:
            req.side_effect = [
                self._auth_ok(),
                _ok({"checkoutForms": [], "count": 0, "totalCount": 0}),
            ]
            c.list_orders()
        headers = req.call_args_list[1].kwargs.get("headers", {})
        assert headers.get("Authorization") == "Bearer at1"
        assert headers.get("Accept") == _ACCEPT_HEADER

    def test_expired_at_endpoint_401_raises(self):
        c = _make_client()
        with patch.object(c._session, "request") as req:
            req.side_effect = [self._auth_ok(), _err(401)]
            with pytest.raises(AllegroAuthError):
                c.list_orders()

    def test_business_error_422_raises_business(self):
        c = _make_client()
        with patch.object(c._session, "request") as req:
            req.side_effect = [self._auth_ok(), _err(422, "bad shipment")]
            with pytest.raises(AllegroBusinessError):
                c.create_shipment(order_id="abc", carrier_id="INPOST", waybill="TRK1")

    def test_business_error_404_raises_business(self):
        c = _make_client()
        with patch.object(c._session, "request") as req:
            req.side_effect = [self._auth_ok(), _err(404)]
            with pytest.raises(AllegroBusinessError):
                c.get_order("nope")

    def test_500_raises_server_error(self):
        c = _make_client()
        with patch.object(c._session, "request") as req:
            req.side_effect = [self._auth_ok(), _err(500, "oops")]
            with pytest.raises(CourierServerError):
                c.list_orders()

    def test_timeout_raises_courier_timeout(self):
        c = _make_client()
        with patch.object(c._session, "request") as req:
            req.side_effect = [self._auth_ok(), requests.Timeout("t/o")]
            with pytest.raises(CourierTimeoutError):
                c.list_orders()

    def test_connection_error_raises_courier_connection(self):
        c = _make_client()
        with patch.object(c._session, "request") as req:
            req.side_effect = [self._auth_ok(), requests.ConnectionError("conn")]
            with pytest.raises(CourierConnectionError):
                c.list_orders()


# ═══════════════════════════════════════════════════════════════════════════
# 4. Orders API (7 tests)
# ═══════════════════════════════════════════════════════════════════════════


class TestOrdersApi:
    def _auth_ok(self):
        return _ok({"access_token": "at1", "expires_in": 43200})

    def test_list_orders_default_no_params(self):
        c = _make_client()
        with patch.object(c._session, "request") as req:
            req.side_effect = [
                self._auth_ok(),
                _ok({"checkoutForms": [{"id": "o1"}], "count": 1, "totalCount": 1}),
            ]
            forms = c.list_orders()
        assert forms == [{"id": "o1"}]
        call = req.call_args_list[1]
        assert call.args[0].upper() == "GET"
        assert call.args[1] == f"{_BASE_URL_PROD}/order/checkout-forms"

    def test_list_orders_with_status_filter(self):
        c = _make_client()
        with patch.object(c._session, "request") as req:
            req.side_effect = [
                self._auth_ok(),
                _ok({"checkoutForms": [], "count": 0, "totalCount": 0}),
            ]
            c.list_orders(status="READY_FOR_PROCESSING")
        params = req.call_args_list[1].kwargs.get("params", {})
        assert params.get("status") == "READY_FOR_PROCESSING"

    def test_list_orders_with_bought_at_gte(self):
        c = _make_client()
        with patch.object(c._session, "request") as req:
            req.side_effect = [
                self._auth_ok(),
                _ok({"checkoutForms": [], "count": 0, "totalCount": 0}),
            ]
            c.list_orders(bought_at_gte="2026-01-01T00:00:00Z")
        params = req.call_args_list[1].kwargs.get("params", {})
        assert params.get("lineItems.boughtAt.gte") == "2026-01-01T00:00:00Z"

    def test_list_orders_pagination_offset_limit(self):
        c = _make_client()
        with patch.object(c._session, "request") as req:
            req.side_effect = [
                self._auth_ok(),
                _ok({"checkoutForms": [], "count": 0, "totalCount": 0}),
            ]
            c.list_orders(limit=50, offset=100)
        params = req.call_args_list[1].kwargs.get("params", {})
        assert params.get("limit") == 50
        assert params.get("offset") == 100

    def test_get_order_returns_form(self):
        c = _make_client()
        with patch.object(c._session, "request") as req:
            req.side_effect = [self._auth_ok(), _ok({"id": "o1", "buyer": {"login": "b"}})]
            data = c.get_order("o1")
        assert data["id"] == "o1"
        call = req.call_args_list[1]
        assert call.args[1] == f"{_BASE_URL_PROD}/order/checkout-forms/o1"

    def test_mark_order_processed_puts_fulfillment(self):
        c = _make_client()
        with patch.object(c._session, "request") as req:
            req.side_effect = [self._auth_ok(), _ok({}, status=204)]
            c.mark_order_processed("o1")
        call = req.call_args_list[1]
        assert call.args[0].upper() == "PUT"
        assert call.args[1] == f"{_BASE_URL_PROD}/order/checkout-forms/o1/fulfillment"
        body = call.kwargs.get("json") or {}
        assert body.get("status") in {"PROCESSING", "SENT", "READY_FOR_SHIPMENT"}

    def test_list_orders_handles_empty_response(self):
        c = _make_client()
        with patch.object(c._session, "request") as req:
            req.side_effect = [self._auth_ok(), _ok({})]
            assert c.list_orders() == []


# ═══════════════════════════════════════════════════════════════════════════
# 5. Shipments API (3 tests)
# ═══════════════════════════════════════════════════════════════════════════


class TestShipmentsApi:
    def _auth_ok(self):
        return _ok({"access_token": "at1", "expires_in": 43200})

    def test_create_shipment_posts_carrier_and_waybill(self):
        c = _make_client()
        with patch.object(c._session, "request") as req:
            req.side_effect = [self._auth_ok(), _ok({"id": "ship1"})]
            r = c.create_shipment(order_id="o1", carrier_id="INPOST", waybill="TRK1")
        assert r["id"] == "ship1"
        call = req.call_args_list[1]
        assert call.args[0].upper() == "POST"
        assert call.args[1] == f"{_BASE_URL_PROD}/order/checkout-forms/o1/shipments"
        body = call.kwargs.get("json") or {}
        assert body["carrierId"] == "INPOST"
        assert body["waybill"] == "TRK1"

    def test_get_shipments_returns_list(self):
        c = _make_client()
        with patch.object(c._session, "request") as req:
            req.side_effect = [self._auth_ok(), _ok({"shipments": [{"id": "s1"}]})]
            r = c.get_shipments("o1")
        assert r == [{"id": "s1"}]

    def test_get_shipments_empty_when_absent(self):
        c = _make_client()
        with patch.object(c._session, "request") as req:
            req.side_effect = [self._auth_ok(), _ok({})]
            assert c.get_shipments("o1") == []


# ═══════════════════════════════════════════════════════════════════════════
# 6. Invoices API (5 tests)
# ═══════════════════════════════════════════════════════════════════════════


class TestInvoicesApi:
    def _auth_ok(self):
        return _ok({"access_token": "at1", "expires_in": 43200})

    def test_list_order_invoices_returns_list(self):
        c = _make_client()
        with patch.object(c._session, "request") as req:
            req.side_effect = [self._auth_ok(), _ok({"invoices": [{"id": "inv1"}]})]
            r = c.list_order_invoices("o1")
        assert r == [{"id": "inv1"}]
        call = req.call_args_list[1]
        assert call.args[1] == f"{_BASE_URL_PROD}/order/checkout-forms/o1/invoices"

    def test_create_invoice_declaration_posts_metadata(self):
        c = _make_client()
        with patch.object(c._session, "request") as req:
            req.side_effect = [self._auth_ok(), _ok({"id": "inv1"})]
            r = c.create_invoice_declaration(
                order_id="o1", invoice_number="FV/1/2026", file_type="VAT"
            )
        assert r["id"] == "inv1"
        call = req.call_args_list[1]
        assert call.args[0].upper() == "POST"
        assert call.args[1] == f"{_BASE_URL_PROD}/order/checkout-forms/o1/invoices"
        body = call.kwargs.get("json") or {}
        # exact payload shape is Allegro-specific; we assert core fields exist
        assert "file" in body or "invoiceNumber" in body

    def test_upload_invoice_file_puts_pdf_bytes(self):
        c = _make_client()
        with patch.object(c._session, "request") as req:
            req.side_effect = [self._auth_ok(), _ok({}, status=204)]
            c.upload_invoice_file(order_id="o1", invoice_id="inv1", pdf_bytes=b"%PDF-1.4 test")
        call = req.call_args_list[1]
        assert call.args[0].upper() == "PUT"
        assert (
            call.args[1]
            == f"{_BASE_URL_PROD}/order/checkout-forms/o1/invoices/inv1/file"
        )
        # Body should be raw PDF bytes with application/pdf content-type
        body = call.kwargs.get("data")
        assert body == b"%PDF-1.4 test"
        headers = call.kwargs.get("headers", {})
        assert headers.get("Content-Type") == "application/pdf"

    def test_list_order_invoices_empty(self):
        c = _make_client()
        with patch.object(c._session, "request") as req:
            req.side_effect = [self._auth_ok(), _ok({})]
            assert c.list_order_invoices("o1") == []

    def test_upload_invoice_file_500_raises_server_error(self):
        c = _make_client()
        with patch.object(c._session, "request") as req:
            req.side_effect = [self._auth_ok(), _err(500)]
            with pytest.raises(CourierServerError):
                c.upload_invoice_file(order_id="o1", invoice_id="inv1", pdf_bytes=b"x")
