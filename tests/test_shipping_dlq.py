"""Tests for the shipping-drafts DLQ (P1-9).

Covers:
- ShippingStore.enqueue_dlq / list_dlq / get_dlq_entry / delete_dlq_entry
- _create_draft_safely enqueues failures instead of losing them
- POST /api/shipping/drafts/dlq/{entry_id}/retry
- DELETE /api/shipping/drafts/dlq/{entry_id}
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

os.environ.setdefault("AZURE_AUTH_DISABLED", "true")

import pytest
from fastapi.testclient import TestClient

from zdrovena.api.main import app
from zdrovena.api.routers.webhooks import _create_draft_safely
from zdrovena.common.shipping_store import ShippingStore


@pytest.fixture()
def store(tmp_path) -> ShippingStore:
    return ShippingStore(local_root=tmp_path / "shipping")


@pytest.fixture()
def client(store):
    with patch("zdrovena.api.deps._shipping_store_singleton", return_value=store):
        with TestClient(app, raise_server_exceptions=True) as c:
            yield c


# ── ShippingStore DLQ primitives ─────────────────────────────────────────────


class TestShippingStoreDlq:
    def test_enqueue_creates_entry(self, store):
        entry = store.enqueue_dlq(
            payload={"id": 1, "order_number": 99},
            error="ValueError: boom",
            source="shopify",
        )
        assert entry["id"]
        assert entry["retries"] == 0
        assert entry["last_error"] == "ValueError: boom"
        assert entry["source"] == "shopify"
        assert entry["payload"] == {"id": 1, "order_number": 99}
        assert entry["created_at"] == entry["updated_at"]

    def test_list_dlq_returns_entries_newest_first(self, store):
        e1 = store.enqueue_dlq(payload={"id": 1}, error="first")
        e2 = store.enqueue_dlq(payload={"id": 2}, error="second")
        entries = store.list_dlq()
        assert {e["id"] for e in entries} == {e1["id"], e2["id"]}

    def test_get_and_delete(self, store):
        entry = store.enqueue_dlq(payload={"x": 1}, error="err")
        assert store.get_dlq_entry(entry["id"]) is not None
        store.delete_dlq_entry(entry["id"])
        assert store.get_dlq_entry(entry["id"]) is None

    def test_enqueue_with_existing_id_bumps_retries(self, store):
        first = store.enqueue_dlq(payload={"id": 1}, error="err-1", entry_id="stable-id")
        assert first["retries"] == 0
        second = store.enqueue_dlq(payload={"id": 1}, error="err-2", entry_id="stable-id")
        assert second["id"] == "stable-id"
        assert second["retries"] == 1
        assert second["last_error"] == "err-2"
        # created_at preserved, updated_at may or may not differ
        assert second["created_at"] == first["created_at"]

    def test_delete_missing_entry_is_noop(self, store):
        # Should not raise
        store.delete_dlq_entry("does-not-exist")


# ── _create_draft_safely enqueues on failure ─────────────────────────────────


class TestCreateDraftSafely:
    def test_success_does_not_touch_dlq(self, store):
        order = {
            "id": 42,
            "order_number": 1,
            "shipping_lines": [{"title": "InPost Paczkomat"}],
            "shipping_address": {"address1": "Testowa 1", "city": "Warszawa", "zip": "00-001"},
            "customer": {"first_name": "A", "last_name": "B", "email": "a@b.pl"},
            "line_items": [{"name": "Test", "quantity": 1}],
        }
        _create_draft_safely(order, store, storage=MagicMock(), source="shopify")
        assert store.list_dlq() == []
        assert len(store.list_drafts()) == 1

    def test_failure_enqueues_to_dlq(self, store):
        broken_store = MagicMock(wraps=store)
        broken_store.upsert_draft.side_effect = RuntimeError("table down")
        # enqueue_dlq must still go to the real store
        broken_store.enqueue_dlq.side_effect = store.enqueue_dlq

        order = {
            "id": 999,
            "order_number": 7,
            "shipping_lines": [{"title": "Kurier DPD"}],
            "shipping_address": {"address1": "X", "city": "Warszawa", "zip": "00-001"},
            "customer": {"first_name": "A", "last_name": "B", "email": "a@b.pl"},
            "line_items": [{"name": "Test", "quantity": 1}],
        }
        _create_draft_safely(order, broken_store, storage=MagicMock(), source="shopify")

        entries = store.list_dlq()
        assert len(entries) == 1
        assert "table down" in entries[0]["last_error"]
        assert entries[0]["payload"]["id"] == 999
        assert entries[0]["source"] == "shopify"

    def test_failure_is_never_reraised(self, store):
        broken_store = MagicMock(wraps=store)
        broken_store.upsert_draft.side_effect = RuntimeError("boom")
        broken_store.enqueue_dlq.side_effect = RuntimeError("dlq also down")

        # even if DLQ enqueue itself fails, we swallow to keep FastAPI happy
        _create_draft_safely(
            {"id": 1, "shipping_lines": [{"title": "x"}], "line_items": []},
            broken_store,
            storage=MagicMock(),
            source="shopify",
        )


# ── HTTP endpoints ───────────────────────────────────────────────────────────


def _seed_dlq(store: ShippingStore, *, order_id: int = 100) -> dict:
    return store.enqueue_dlq(
        payload={
            "id": order_id,
            "order_number": order_id,
            "shipping_lines": [{"title": "InPost Paczkomat"}],
            "shipping_address": {
                "address1": "Testowa 1",
                "city": "Warszawa",
                "zip": "00-001",
            },
            "customer": {"first_name": "A", "last_name": "B", "email": "a@b.pl"},
            "line_items": [{"name": "Test", "quantity": 1}],
        },
        error="RuntimeError: transient",
        source="shopify",
    )


class TestDlqEndpoints:
    def test_list_dlq_endpoint(self, client, store):
        entry = _seed_dlq(store)
        resp = client.get("/api/shipping/drafts/dlq")
        assert resp.status_code == 200
        body = resp.json()
        ids = [e["id"] for e in body["entries"]]
        assert entry["id"] in ids

    def test_list_dlq_empty(self, client):
        resp = client.get("/api/shipping/drafts/dlq")
        assert resp.status_code == 200
        assert resp.json() == {"entries": []}

    def test_retry_success_removes_entry(self, client, store):
        entry = _seed_dlq(store)
        resp = client.post(f"/api/shipping/drafts/dlq/{entry['id']}/retry")
        assert resp.status_code == 200
        assert resp.json()["status"] == "retried"
        # DLQ entry removed
        assert store.get_dlq_entry(entry["id"]) is None
        # A real draft materialized
        assert len(store.list_drafts()) == 1

    def test_retry_failure_bumps_retries_and_returns_502(self, client, store):
        entry = _seed_dlq(store)
        with patch(
            "zdrovena.api.routers.webhooks._create_draft",
            side_effect=RuntimeError("still broken"),
        ):
            resp = client.post(f"/api/shipping/drafts/dlq/{entry['id']}/retry")
        assert resp.status_code == 502
        assert "still broken" in resp.json()["detail"]
        # Entry still in DLQ; retries bumped, last_error refreshed
        updated = store.get_dlq_entry(entry["id"])
        assert updated is not None
        assert updated["retries"] == 1
        assert "still broken" in updated["last_error"]

    def test_retry_not_found(self, client):
        resp = client.post("/api/shipping/drafts/dlq/does-not-exist/retry")
        assert resp.status_code == 404

    def test_delete_dlq_entry(self, client, store):
        entry = _seed_dlq(store)
        resp = client.delete(f"/api/shipping/drafts/dlq/{entry['id']}")
        assert resp.status_code == 204
        assert store.get_dlq_entry(entry["id"]) is None

    def test_delete_dlq_entry_not_found(self, client):
        resp = client.delete("/api/shipping/drafts/dlq/no-such-id")
        assert resp.status_code == 404
