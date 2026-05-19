"""Tests for zdrovena.api.routers.webhooks — HMAC validation and courier routing."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("AZURE_AUTH_DISABLED", "true")

from zdrovena.api.main import app
from zdrovena.api.routers.webhooks import _pick_courier, _verify_shopify_hmac

# ── Pure helpers ──────────────────────────────────────────────────────────────


def _sign(body: bytes, secret: str) -> str:
    return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


class TestVerifyShopifyHmac:
    def test_valid_signature(self):
        body = b'{"id":1}'
        sig = _sign(body, "my-secret")
        assert _verify_shopify_hmac(body, sig, "my-secret") is True

    def test_invalid_signature(self):
        body = b'{"id":1}'
        assert _verify_shopify_hmac(body, "not-valid", "my-secret") is False

    def test_wrong_secret(self):
        body = b'{"id":1}'
        sig = _sign(body, "correct-secret")
        assert _verify_shopify_hmac(body, sig, "wrong-secret") is False

    def test_tampered_body(self):
        body = b'{"id":1}'
        sig = _sign(body, "secret")
        assert _verify_shopify_hmac(b'{"id":2}', sig, "secret") is False


class TestPickCourier:
    def test_paczkomat_keyword_routes_to_inpost(self):
        order = {"shipping_lines": [{"title": "InPost Paczkomat 24"}]}
        assert _pick_courier(order) == "inpost"

    def test_kurier_keyword_routes_to_inpost(self):
        order = {"shipping_lines": [{"title": "InPost Kurier ekspresowy"}]}
        assert _pick_courier(order) == "inpost"

    def test_dpd_routes_to_apaczka(self):
        order = {"shipping_lines": [{"title": "Wysyłka DPD"}]}
        assert _pick_courier(order) == "apaczka"

    def test_unknown_title_routes_to_apaczka(self):
        order = {"shipping_lines": [{"title": "Odbiór osobisty"}]}
        assert _pick_courier(order) == "apaczka"

    def test_empty_shipping_lines_defaults_to_apaczka(self):
        assert _pick_courier({"shipping_lines": []}) == "apaczka"

    def test_missing_shipping_lines_defaults_to_apaczka(self):
        assert _pick_courier({}) == "apaczka"

    def test_case_insensitive(self):
        order = {"shipping_lines": [{"title": "INPOST PACZKOMAT"}]}
        assert _pick_courier(order) == "inpost"


# ── Webhook endpoint ──────────────────────────────────────────────────────────


@pytest.fixture()
def client(tmp_path):
    from zdrovena.common.storage import LocalStorageService
    storage = LocalStorageService(root=tmp_path / "storage")
    with patch("zdrovena.api.deps._storage_singleton", return_value=storage):
        with TestClient(app, raise_server_exceptions=True) as c:
            yield c


_ORDER_NO_SHIPPING = json.dumps({"id": 999, "order_number": 1001}).encode()
_ORDER_WITH_SHIPPING = json.dumps({
    "id": 1,
    "order_number": 1042,
    "shipping_lines": [{"title": "DPD Kurier"}],
    "shipping_address": {"first_name": "Jan", "last_name": "Kowalski",
                         "address1": "Kwiatowa 1", "city": "Warszawa", "zip": "00-001"},
    "customer": {"email": "jan@example.com", "phone": "500000000"},
}).encode()


class TestWebhookEndpoint:
    def test_no_shipping_lines_returns_skipped(self, client):
        with patch("zdrovena.api.routers.webhooks._get_webhook_secret", return_value=None):
            resp = client.post(
                "/api/webhooks/shopify/order-created",
                content=_ORDER_NO_SHIPPING,
                headers={"Content-Type": "application/json"},
            )
        assert resp.status_code == 200
        assert resp.json() == {"status": "skipped"}

    def test_no_secret_configured_skips_hmac(self, client):
        with patch("zdrovena.api.routers.webhooks._get_webhook_secret", return_value=None):
            with patch("zdrovena.api.routers.webhooks._create_draft"):
                resp = client.post(
                    "/api/webhooks/shopify/order-created",
                    content=_ORDER_WITH_SHIPPING,
                    headers={"Content-Type": "application/json"},
                )
        assert resp.status_code == 200
        assert resp.json() == {"status": "accepted"}

    def test_valid_hmac_accepted(self, client):
        secret = "test-webhook-secret"
        sig = _sign(_ORDER_WITH_SHIPPING, secret)
        with patch("zdrovena.api.routers.webhooks._get_webhook_secret", return_value=secret):
            with patch("zdrovena.api.routers.webhooks._create_draft"):
                resp = client.post(
                    "/api/webhooks/shopify/order-created",
                    content=_ORDER_WITH_SHIPPING,
                    headers={"Content-Type": "application/json", "X-Shopify-Hmac-Sha256": sig},
                )
        assert resp.status_code == 200
        assert resp.json() == {"status": "accepted"}

    def test_invalid_hmac_rejected(self, client):
        secret = "test-webhook-secret"
        with patch("zdrovena.api.routers.webhooks._get_webhook_secret", return_value=secret):
            resp = client.post(
                "/api/webhooks/shopify/order-created",
                content=_ORDER_WITH_SHIPPING,
                headers={"Content-Type": "application/json", "X-Shopify-Hmac-Sha256": "bad"},
            )
        assert resp.status_code == 401

    def test_missing_hmac_header_with_secret_configured_rejected(self, client):
        with patch("zdrovena.api.routers.webhooks._get_webhook_secret", return_value="secret"):
            resp = client.post(
                "/api/webhooks/shopify/order-created",
                content=_ORDER_WITH_SHIPPING,
                headers={"Content-Type": "application/json"},
            )
        assert resp.status_code == 401

    def test_invalid_json_returns_400(self, client):
        with patch("zdrovena.api.routers.webhooks._get_webhook_secret", return_value=None):
            resp = client.post(
                "/api/webhooks/shopify/order-created",
                content=b"not-json",
                headers={"Content-Type": "application/json"},
            )
        assert resp.status_code == 400
