"""Integration tests for invoice preview and create-invoice endpoints.

Covers GET /shipping/drafts/{id}/invoice-preview and
POST /shipping/drafts/{id}/create-invoice.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("AZURE_AUTH_DISABLED", "true")

from zdrovena.api.main import app
from zdrovena.common.shipping_store import ShippingStore
from zdrovena.common.storage import LocalStorageService

# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def store(tmp_path) -> ShippingStore:
    return ShippingStore(local_root=tmp_path / "store")


@pytest.fixture()
def storage(tmp_path) -> LocalStorageService:
    return LocalStorageService(root=tmp_path / "storage")


@pytest.fixture()
def client(store, storage):
    with patch("zdrovena.api.deps._storage_singleton", return_value=storage):
        with patch("zdrovena.api.deps._shipping_store_singleton", return_value=store):
            with TestClient(app, raise_server_exceptions=True) as c:
                yield c


def _make_draft(store: ShippingStore, **overrides) -> str:
    base = {
        "id": overrides.pop("id", "draft-inv-1"),
        "source": "allegro",
        "status": "created",
        "courier": "apaczka",
        "external_order_id": "allegro-uuid-123",
        "shopify_order_number": "allegro-uuid-123",
        "fakturownia_invoice_id": None,
    }
    base.update(overrides)
    store.upsert_draft(base)
    return base["id"]


_MOCK_ORDER = {
    "id": "allegro-uuid-123",
    "buyer": {"firstName": "Jan", "lastName": "Kowalski", "email": "jan@example.com"},
    "lineItems": [
        {"quantity": 2, "offer": {"name": "Woda 1L", "id": "sku-1"}, "price": {"amount": "10.00"}}
    ],
    "delivery": {"address": {}},
    "invoice": {},
}

_MOCK_PAYLOAD = {
    "buyer_name": "Jan Kowalski",
    "buyer_email": "jan@example.com",
    "positions": [{"name": "Woda 1L", "quantity": 2, "total_price_gross": 20.0, "tax_name": "8%"}],
    "settlement_positions": [],
}


# ── GET /invoice-preview ───────────────────────────────────────────────────────


class TestInvoicePreview:
    def test_404_when_draft_not_found(self, client):
        resp = client.get("/api/shipping/drafts/nonexistent/invoice-preview")
        assert resp.status_code == 404

    def test_400_for_non_allegro_draft(self, client, store):
        _make_draft(store, id="shopify-draft", source="shopify")
        resp = client.get("/api/shipping/drafts/shopify-draft/invoice-preview")
        assert resp.status_code == 400

    def test_returns_already_created_when_invoice_exists(self, client, store):
        _make_draft(store, fakturownia_invoice_id=42)
        resp = client.get("/api/shipping/drafts/draft-inv-1/invoice-preview")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "already_created"
        assert body["fakturownia_invoice_id"] == 42

    def test_503_when_allegro_not_configured(self, client, store):
        _make_draft(store)
        with patch("zdrovena.api.routers.webhooks._get_allegro_client", return_value=None):
            resp = client.get("/api/shipping/drafts/draft-inv-1/invoice-preview")
        assert resp.status_code == 503

    def test_preview_ready_happy_path(self, client, store):
        _make_draft(store)
        mock_allegro = MagicMock()
        mock_allegro.get_order.return_value = _MOCK_ORDER
        with patch("zdrovena.api.routers.webhooks._get_allegro_client", return_value=mock_allegro):
            with patch(
                "zdrovena.common.allegro_invoice_mapper.allegro_order_to_fakturownia_invoice",
                return_value=_MOCK_PAYLOAD,
            ):
                resp = client.get("/api/shipping/drafts/draft-inv-1/invoice-preview")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "preview_ready"
        assert body["buyer_name"] == "Jan Kowalski"
        assert len(body["positions"]) == 1
        assert body["positions"][0]["quantity"] == 2
        assert body["total_gross"] == pytest.approx(20.0)
        assert isinstance(body["positions"][0]["line_total"], float)

    def test_zero_quantity_line_item_does_not_crash(self, client, store):
        _make_draft(store)
        mock_allegro = MagicMock()
        mock_allegro.get_order.return_value = _MOCK_ORDER
        zero_qty_payload = {
            **_MOCK_PAYLOAD,
            "positions": [
                {"name": "Item", "quantity": 0, "total_price_gross": 0.0, "tax_name": "8%"}
            ],
        }
        with patch("zdrovena.api.routers.webhooks._get_allegro_client", return_value=mock_allegro):
            with patch(
                "zdrovena.common.allegro_invoice_mapper.allegro_order_to_fakturownia_invoice",
                return_value=zero_qty_payload,
            ):
                resp = client.get("/api/shipping/drafts/draft-inv-1/invoice-preview")
        assert resp.status_code == 200
        body = resp.json()
        assert body["positions"][0]["unit_price_gross"] == 0.0

    def test_settlement_amount_is_float(self, client, store):
        _make_draft(store)
        mock_allegro = MagicMock()
        mock_allegro.get_order.return_value = _MOCK_ORDER
        payload_with_settlement = {
            **_MOCK_PAYLOAD,
            "settlement_positions": [{"description": "Kaucja", "amount": "5.50"}],
        }
        with patch("zdrovena.api.routers.webhooks._get_allegro_client", return_value=mock_allegro):
            with patch(
                "zdrovena.common.allegro_invoice_mapper.allegro_order_to_fakturownia_invoice",
                return_value=payload_with_settlement,
            ):
                resp = client.get("/api/shipping/drafts/draft-inv-1/invoice-preview")
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body["settlement_positions"][0]["amount"], float)
        assert body["settlement_positions"][0]["amount"] == pytest.approx(5.5)


# ── POST /create-invoice ──────────────────────────────────────────────────────


class TestCreateInvoice:
    def test_404_when_draft_not_found(self, client):
        resp = client.post("/api/shipping/drafts/nonexistent/create-invoice")
        assert resp.status_code == 404

    def test_400_for_non_allegro_draft(self, client, store):
        _make_draft(store, id="shopify-draft", source="shopify")
        resp = client.post("/api/shipping/drafts/shopify-draft/create-invoice")
        assert resp.status_code == 400

    def test_returns_already_created_when_invoice_exists(self, client, store):
        _make_draft(store, fakturownia_invoice_id=99)
        resp = client.post("/api/shipping/drafts/draft-inv-1/create-invoice")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "already_created"
        assert body["fakturownia_invoice_id"] == 99

    def test_409_when_pending(self, client, store):
        _make_draft(store, fakturownia_invoice_id="pending")
        resp = client.post("/api/shipping/drafts/draft-inv-1/create-invoice")
        assert resp.status_code == 409

    def test_503_when_allegro_not_configured(self, client, store):
        _make_draft(store)
        with patch("zdrovena.api.routers.webhooks._get_allegro_client", return_value=None):
            with patch(
                "zdrovena.api.routers.webhooks._get_fakturownia_invoice_client",
                return_value=MagicMock(),
            ):
                resp = client.post("/api/shipping/drafts/draft-inv-1/create-invoice")
        assert resp.status_code == 503
        # Pending marker must be cleared
        draft = store.get_draft("draft-inv-1")
        assert not draft.get("fakturownia_invoice_id")

    def test_503_when_fakturownia_not_configured(self, client, store):
        _make_draft(store)
        with patch("zdrovena.api.routers.webhooks._get_allegro_client", return_value=MagicMock()):
            with patch(
                "zdrovena.api.routers.webhooks._get_fakturownia_invoice_client",
                return_value=None,
            ):
                resp = client.post("/api/shipping/drafts/draft-inv-1/create-invoice")
        assert resp.status_code == 503
        draft = store.get_draft("draft-inv-1")
        assert not draft.get("fakturownia_invoice_id")

    def test_happy_path_stores_invoice_id(self, client, store):
        _make_draft(store)
        mock_allegro = MagicMock()
        mock_allegro.get_order.return_value = _MOCK_ORDER
        mock_fakturownia = MagicMock()
        creation_result = {"status": "created", "fakturownia_invoice_id": 777}
        with patch("zdrovena.api.routers.webhooks._get_allegro_client", return_value=mock_allegro):
            with patch(
                "zdrovena.api.routers.webhooks._get_fakturownia_invoice_client",
                return_value=mock_fakturownia,
            ):
                with patch(
                    "zdrovena.api.routers.allegro_invoicer.create_invoice_for_order",
                    return_value=creation_result,
                ):
                    resp = client.post("/api/shipping/drafts/draft-inv-1/create-invoice")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "created"
        assert body["fakturownia_invoice_id"] == 777
        # Draft must be updated with the real invoice ID
        draft = store.get_draft("draft-inv-1")
        assert draft["fakturownia_invoice_id"] == 777

    def test_502_and_clears_pending_on_creation_failure(self, client, store):
        _make_draft(store)
        mock_allegro = MagicMock()
        mock_allegro.get_order.return_value = _MOCK_ORDER
        failure_result = {"status": "error", "error": "Fakturownia returned 503"}
        with patch("zdrovena.api.routers.webhooks._get_allegro_client", return_value=mock_allegro):
            with patch(
                "zdrovena.api.routers.webhooks._get_fakturownia_invoice_client",
                return_value=MagicMock(),
            ):
                with patch(
                    "zdrovena.api.routers.allegro_invoicer.create_invoice_for_order",
                    return_value=failure_result,
                ):
                    resp = client.post("/api/shipping/drafts/draft-inv-1/create-invoice")
        assert resp.status_code == 502
        # Pending marker must be cleared so user can retry
        draft = store.get_draft("draft-inv-1")
        assert not draft.get("fakturownia_invoice_id")
