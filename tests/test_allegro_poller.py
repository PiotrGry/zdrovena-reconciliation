"""Tests for zdrovena.api.routers.allegro_poller.poll_orders_once.

The poller:
1. Calls AllegroClient.list_orders(status="READY_FOR_PROCESSING")
2. For each order, checks idempotency (external_order_id + source="allegro")
3. Maps Allegro form → shopify-like dict via allegro_to_shopify_order
4. Passes it to _create_draft which persists a draft with source="allegro"
5. Marks order processed via AllegroClient.mark_order_processed

Failure of one order does NOT block others.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from zdrovena.api.routers.allegro_poller import poll_orders_once


def _form(order_id: str, sku: str = "HUMIO-PET-6-001") -> dict:
    return {
        "id": order_id,
        "buyer": {
            "email": "b@example.com",
            "firstName": "Anna",
            "lastName": "Nowak",
            "phoneNumber": "+48 601 000 000",
        },
        "delivery": {
            "address": {
                "firstName": "Anna",
                "lastName": "Nowak",
                "street": "ul. Kwiatowa 5",
                "city": "Warszawa",
                "zipCode": "00-001",
                "phoneNumber": "+48601000000",
                "countryCode": "PL",
            },
            "method": {"name": "InPost Paczkomaty 24/7", "id": "m1"},
            "pickupPoint": {"id": "WAW123A"},
            "cost": {"amount": "0.00", "currency": "PLN"},
        },
        "lineItems": [
            {
                "id": "li1",
                "offer": {
                    "id": "off1",
                    "name": "HUMIO 6 PET",
                    "external": {"id": sku},
                },
                "quantity": 1,
                "boughtAt": "2026-06-01T10:00:00Z",
            }
        ],
    }


class TestPollOrdersOnce:
    def test_no_orders_returns_zero(self):
        client = MagicMock()
        client.list_orders.return_value = []
        store = MagicMock()
        store.list_drafts.return_value = []
        stats = poll_orders_once(client=client, shipping_store=store, storage=MagicMock())
        assert stats["fetched"] == 0
        assert stats["created"] == 0
        client.mark_order_processed.assert_not_called()

    def test_creates_draft_default_does_not_mark_processed(self, monkeypatch):
        """Bezpieczny default: sam draft ≠ nadanie — nie oznaczamy PROCESSING."""
        monkeypatch.delenv("ALLEGRO_MARK_ON_DRAFT", raising=False)
        client = MagicMock()
        client.list_orders.return_value = [_form("af1")]
        store = MagicMock()
        store.list_drafts.return_value = []
        poll_orders_once(client=client, shipping_store=store, storage=MagicMock())
        assert store.upsert_draft.call_count == 1
        saved = store.upsert_draft.call_args.args[0]
        assert saved["source"] == "allegro"
        assert saved["external_order_id"] == "af1"
        client.mark_order_processed.assert_not_called()

    def test_creates_draft_and_marks_processed_when_flag_set(self, monkeypatch):
        """Za flagą ALLEGRO_MARK_ON_DRAFT=1 — zachowanie legacy (mark od razu)."""
        monkeypatch.setenv("ALLEGRO_MARK_ON_DRAFT", "1")
        client = MagicMock()
        client.list_orders.return_value = [_form("af1")]
        store = MagicMock()
        store.list_drafts.return_value = []
        poll_orders_once(client=client, shipping_store=store, storage=MagicMock())
        client.mark_order_processed.assert_called_once_with("af1")

    def test_idempotency_skips_existing_allegro_draft(self):
        client = MagicMock()
        client.list_orders.return_value = [_form("af1")]
        store = MagicMock()
        store.list_drafts.return_value = [
            {"source": "allegro", "external_order_id": "af1", "status": "pending"}
        ]
        stats = poll_orders_once(client=client, shipping_store=store, storage=MagicMock())
        assert stats["skipped_duplicate"] == 1
        store.upsert_draft.assert_not_called()
        # Still safe to (re-)ack Allegro side so it drops from the queue
        # — but only if not already ack'd. Poller MAY choose to skip mark:
        # we tolerate either.

    def test_error_draft_is_retried(self):
        client = MagicMock()
        client.list_orders.return_value = [_form("af1")]
        store = MagicMock()
        store.list_drafts.return_value = [
            {"source": "allegro", "external_order_id": "af1", "status": "error"}
        ]
        poll_orders_once(client=client, shipping_store=store, storage=MagicMock())
        assert store.upsert_draft.call_count == 1

    def test_shopify_draft_with_same_id_does_not_shadow(self):
        # A Shopify draft with the same numeric id must NOT prevent Allegro create
        client = MagicMock()
        client.list_orders.return_value = [_form("af1")]
        store = MagicMock()
        store.list_drafts.return_value = [
            {"source": "shopify", "external_order_id": "af1", "status": "pending"}
        ]
        poll_orders_once(client=client, shipping_store=store, storage=MagicMock())
        assert store.upsert_draft.call_count == 1

    def test_one_order_failure_does_not_block_others(self, monkeypatch):
        monkeypatch.setenv("ALLEGRO_MARK_ON_DRAFT", "1")
        client = MagicMock()
        client.list_orders.return_value = [_form("af1"), _form("af2"), _form("af3")]
        store = MagicMock()
        store.list_drafts.return_value = []
        # simulate storage error on second upsert
        store.upsert_draft.side_effect = [None, RuntimeError("boom"), None]
        stats = poll_orders_once(client=client, shipping_store=store, storage=MagicMock())
        assert stats["created"] == 2
        assert stats["errors"] == 1
        # mark_order_processed only called for successful ones (with flag)
        marked = [c.args[0] for c in client.mark_order_processed.call_args_list]
        assert "af1" in marked
        assert "af3" in marked
        assert "af2" not in marked

    def test_uses_ready_for_processing_status_filter(self):
        client = MagicMock()
        client.list_orders.return_value = []
        store = MagicMock()
        store.list_drafts.return_value = []
        poll_orders_once(client=client, shipping_store=store, storage=MagicMock())
        kwargs = client.list_orders.call_args.kwargs
        assert kwargs.get("status") == "READY_FOR_PROCESSING"

    def test_list_orders_exception_returns_error_stats(self):
        client = MagicMock()
        client.list_orders.side_effect = RuntimeError("network")
        store = MagicMock()
        stats = poll_orders_once(client=client, shipping_store=store, storage=MagicMock())
        assert stats["errors"] >= 1
        assert stats["created"] == 0

    def test_pagination_stops_when_no_more(self):
        client = MagicMock()
        # single page — poller invokes once for the default cycle
        client.list_orders.return_value = [_form("af1")]
        store = MagicMock()
        store.list_drafts.return_value = []
        poll_orders_once(client=client, shipping_store=store, storage=MagicMock())
        assert client.list_orders.call_count == 1


class TestExternalOrderIdOnDraft:
    def test_external_order_id_set_for_allegro(self):
        client = MagicMock()
        client.list_orders.return_value = [_form("af1")]
        store = MagicMock()
        store.list_drafts.return_value = []
        poll_orders_once(client=client, shipping_store=store, storage=MagicMock())
        saved = store.upsert_draft.call_args.args[0]
        assert saved["external_order_id"] == "af1"

    def test_shopify_order_id_absent_or_matches_external(self):
        client = MagicMock()
        client.list_orders.return_value = [_form("af1")]
        store = MagicMock()
        store.list_drafts.return_value = []
        poll_orders_once(client=client, shipping_store=store, storage=MagicMock())
        saved = store.upsert_draft.call_args.args[0]
        # For Allegro drafts, shopify_order_id may be absent OR mirror external_order_id
        assert saved.get("shopify_order_id") in {None, "af1", ""}


class TestInvoiceCreationWiring:
    def test_calls_invoicer_after_successful_draft_creation(self, monkeypatch):
        monkeypatch.delenv("ALLEGRO_MARK_ON_DRAFT", raising=False)
        client = MagicMock()
        client.list_orders.return_value = [_form("af1")]
        store = MagicMock()
        store.list_drafts.return_value = []
        fakturownia = MagicMock()

        with patch(
            "zdrovena.api.routers.allegro_poller.create_invoice_for_order",
            return_value={"status": "created", "fakturownia_invoice_id": 1},
        ) as mock_invoicer:
            poll_orders_once(
                client=client,
                shipping_store=store,
                storage=MagicMock(),
                fakturownia_client=fakturownia,
            )

        mock_invoicer.assert_called_once()
        called_order = mock_invoicer.call_args.args[0]
        assert called_order["id"] == "af1"
        assert mock_invoicer.call_args.kwargs["fakturownia_client"] is fakturownia
        assert mock_invoicer.call_args.kwargs["allegro_client"] is client

    def test_does_not_call_invoicer_when_draft_creation_fails(self, monkeypatch):
        monkeypatch.delenv("ALLEGRO_MARK_ON_DRAFT", raising=False)
        client = MagicMock()
        client.list_orders.return_value = [_form("af1")]
        store = MagicMock()
        store.list_drafts.return_value = []
        store.upsert_draft.side_effect = RuntimeError("store down")
        fakturownia = MagicMock()

        with patch("zdrovena.api.routers.allegro_poller.create_invoice_for_order") as mock_invoicer:
            poll_orders_once(
                client=client,
                shipping_store=store,
                storage=MagicMock(),
                fakturownia_client=fakturownia,
            )

        mock_invoicer.assert_not_called()

    def test_does_not_call_invoicer_for_skipped_duplicate(self, monkeypatch):
        monkeypatch.delenv("ALLEGRO_MARK_ON_DRAFT", raising=False)
        client = MagicMock()
        client.list_orders.return_value = [_form("af1")]
        store = MagicMock()
        store.list_drafts.return_value = [
            {"source": "allegro", "external_order_id": "af1", "status": "created"}
        ]
        fakturownia = MagicMock()

        with patch("zdrovena.api.routers.allegro_poller.create_invoice_for_order") as mock_invoicer:
            poll_orders_once(
                client=client,
                shipping_store=store,
                storage=MagicMock(),
                fakturownia_client=fakturownia,
            )

        mock_invoicer.assert_not_called()

    def test_invoicer_failure_does_not_abort_cycle(self, monkeypatch):
        """One order's invoice failing must not block the next order's draft."""
        monkeypatch.delenv("ALLEGRO_MARK_ON_DRAFT", raising=False)
        client = MagicMock()
        client.list_orders.return_value = [_form("af1"), _form("af2")]
        store = MagicMock()
        store.list_drafts.return_value = []
        fakturownia = MagicMock()

        with patch(
            "zdrovena.api.routers.allegro_poller.create_invoice_for_order",
            side_effect=RuntimeError("boom"),
        ):
            stats = poll_orders_once(
                client=client,
                shipping_store=store,
                storage=MagicMock(),
                fakturownia_client=fakturownia,
            )

        assert stats["created"] == 2
        assert stats["invoice_errors"] == 2

    def test_missing_fakturownia_client_skips_invoicing_gracefully(self, monkeypatch):
        """fakturownia_client is optional — callers not ready to wire it up
        yet (or environments without Fakturownia credentials) must not crash.
        """
        monkeypatch.delenv("ALLEGRO_MARK_ON_DRAFT", raising=False)
        client = MagicMock()
        client.list_orders.return_value = [_form("af1")]
        store = MagicMock()
        store.list_drafts.return_value = []

        with patch("zdrovena.api.routers.allegro_poller.create_invoice_for_order") as mock_invoicer:
            stats = poll_orders_once(client=client, shipping_store=store, storage=MagicMock())

        mock_invoicer.assert_not_called()
        assert stats["created"] == 1
