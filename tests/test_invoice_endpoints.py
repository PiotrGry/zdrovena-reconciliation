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
    "positions": [{"name": "Woda 1L", "quantity": 2, "total_price_gross": 20.0, "tax": 8}],
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

    def test_returns_retry_ready_when_automatic_invoice_is_incomplete(self, client, store):
        _make_draft(
            store,
            fakturownia_invoice_id=42,
            fakturownia_invoice_error="Allegro PDF upload failed",
        )

        resp = client.get("/api/shipping/drafts/draft-inv-1/invoice-preview")

        assert resp.status_code == 200
        assert resp.json() == {
            "status": "retry_ready",
            "fakturownia_invoice_id": 42,
            "error": "Allegro PDF upload failed",
        }

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
        # PR-14: vat_rate must reflect the position's actual `tax` (int percent),
        # not the old always-"8%" bug from reading a nonexistent `tax_name`.
        assert body["positions"][0]["vat_rate"] == "8%"
        assert body["total_gross"] == pytest.approx(20.0)
        assert body["positions_total"] == pytest.approx(20.0)
        assert body["settlement_total"] == pytest.approx(0.0)
        assert isinstance(body["positions"][0]["line_total"], float)

    def test_zero_quantity_line_item_does_not_crash(self, client, store):
        _make_draft(store)
        mock_allegro = MagicMock()
        mock_allegro.get_order.return_value = _MOCK_ORDER
        zero_qty_payload = {
            **_MOCK_PAYLOAD,
            "positions": [{"name": "Item", "quantity": 0, "total_price_gross": 0.0, "tax": 8}],
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

    def test_matches_allegro_total_to_pay(self, client, store):
        """PR-14: preview compares 'Do zapłaty' (positions + kaucja) against
        Allegro summary.totalToPay minus delivery, so the operator can verify."""
        _make_draft(store)
        order_with_summary = {
            **_MOCK_ORDER,
            "summary": {"totalToPay": {"amount": "32.50", "currency": "PLN"}},
            "delivery": {"cost": {"amount": "12.50"}},
        }
        payload = {
            **_MOCK_PAYLOAD,
            "settlement_positions": [{"description": "Kaucja", "amount": "0.00"}],
        }
        mock_allegro = MagicMock()
        mock_allegro.get_order.return_value = order_with_summary
        with patch("zdrovena.api.routers.webhooks._get_allegro_client", return_value=mock_allegro):
            with patch(
                "zdrovena.common.allegro_invoice_mapper.allegro_order_to_fakturownia_invoice",
                return_value=payload,
            ):
                resp = client.get("/api/shipping/drafts/draft-inv-1/invoice-preview")
        assert resp.status_code == 200
        body = resp.json()
        # totalToPay 32.50 - delivery 12.50 = 20.00 == positions_total 20.00
        assert body["allegro_total_to_pay"] == pytest.approx(20.0)
        assert body["matches_allegro"] is True
        # R4.3: explainable difference is present and zero when it matches.
        assert body["difference"] == pytest.approx(0.0)

    def test_mismatch_with_allegro_flagged(self, client, store):
        _make_draft(store)
        order_with_summary = {
            **_MOCK_ORDER,
            "summary": {"totalToPay": {"amount": "99.00", "currency": "PLN"}},
            "delivery": {"cost": {"amount": "0.00"}},
        }
        mock_allegro = MagicMock()
        mock_allegro.get_order.return_value = order_with_summary
        with patch("zdrovena.api.routers.webhooks._get_allegro_client", return_value=mock_allegro):
            with patch(
                "zdrovena.common.allegro_invoice_mapper.allegro_order_to_fakturownia_invoice",
                return_value=_MOCK_PAYLOAD,
            ):
                resp = client.get("/api/shipping/drafts/draft-inv-1/invoice-preview")
        body = resp.json()
        # 99.00 != positions 20.00 → mismatch flagged, but still preview_ready
        assert body["status"] == "preview_ready"
        assert body["matches_allegro"] is False
        # R4.3: difference is the signed, explainable delta ( our 20.00 − 99.00).
        assert body["difference"] == pytest.approx(-79.0)


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

    def test_already_exists_recovers_id_and_returns_success(self, client, store):
        """PR-11: when Fakturownia already has the invoice for this order,
        the endpoint must recover the id, persist it, and return 200
        already_created — never 502, never reset state to None (the loop bug)."""
        _make_draft(store)
        mock_allegro = MagicMock()
        mock_allegro.get_order.return_value = _MOCK_ORDER
        already_result = {
            "status": "already_exists",
            "fakturownia_invoice_id": 555,
            "fakturownia_invoice_number": "FV/2026/555",
        }
        with patch("zdrovena.api.routers.webhooks._get_allegro_client", return_value=mock_allegro):
            with patch(
                "zdrovena.api.routers.webhooks._get_fakturownia_invoice_client",
                return_value=MagicMock(),
            ):
                with patch(
                    "zdrovena.api.routers.allegro_invoicer.create_invoice_for_order",
                    return_value=already_result,
                ):
                    resp = client.post("/api/shipping/drafts/draft-inv-1/create-invoice")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "already_created"
        assert body["fakturownia_invoice_id"] == 555
        assert body["fakturownia_invoice_number"] == "FV/2026/555"
        draft = store.get_draft("draft-inv-1")
        assert draft["fakturownia_invoice_id"] == 555

    def test_manual_fallback_resumes_an_incomplete_automatic_invoice(self, client, store):
        _make_draft(
            store,
            fakturownia_invoice_id=555,
            fakturownia_invoice_error="Allegro PDF upload failed",
        )
        mock_allegro = MagicMock()
        mock_allegro.get_order.return_value = _MOCK_ORDER
        recovered = {
            "status": "already_exists",
            "fakturownia_invoice_id": 555,
            "fakturownia_invoice_number": "FV/2026/555",
        }
        with patch("zdrovena.api.routers.webhooks._get_allegro_client", return_value=mock_allegro):
            with patch(
                "zdrovena.api.routers.webhooks._get_fakturownia_invoice_client",
                return_value=MagicMock(),
            ):
                with patch(
                    "zdrovena.api.routers.allegro_invoicer.create_invoice_for_order",
                    return_value=recovered,
                ) as invoicer:
                    resp = client.post("/api/shipping/drafts/draft-inv-1/create-invoice")

        assert resp.status_code == 200
        assert resp.json()["status"] == "already_created"
        invoicer.assert_called_once()
        draft = store.get_draft("draft-inv-1")
        assert draft["fakturownia_invoice_id"] == 555
        assert draft["fakturownia_invoice_error"] is None

    def test_error_with_recovered_id_preserves_it(self, client, store):
        """PR-11: if Fakturownia created the invoice but a later step (Allegro
        push) failed, the recovered id must be persisted rather than reset to
        None, so a retry attaches to the same document instead of orphaning it."""
        _make_draft(store)
        mock_allegro = MagicMock()
        mock_allegro.get_order.return_value = _MOCK_ORDER
        failure_with_id = {
            "status": "error",
            "error": "Allegro push failed",
            "fakturownia_invoice_id": 888,
        }
        with patch("zdrovena.api.routers.webhooks._get_allegro_client", return_value=mock_allegro):
            with patch(
                "zdrovena.api.routers.webhooks._get_fakturownia_invoice_client",
                return_value=MagicMock(),
            ):
                with patch(
                    "zdrovena.api.routers.allegro_invoicer.create_invoice_for_order",
                    return_value=failure_with_id,
                ):
                    resp = client.post("/api/shipping/drafts/draft-inv-1/create-invoice")
        assert resp.status_code == 502
        draft = store.get_draft("draft-inv-1")
        assert draft["fakturownia_invoice_id"] == 888
