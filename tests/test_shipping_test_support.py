"""Tests for the E2E-only shipping endpoints (``zdrovena/api/routers/shipping/test_support.py``).

Covers:
- POST /api/__test__/shipping/reset
- POST /api/__test__/shipping/drafts
- POST /api/__test__/shipping/dlq
including the fail-closed 404 outside fake non-production mode.
"""

from __future__ import annotations

import os
from unittest.mock import patch

os.environ.setdefault("AZURE_AUTH_DISABLED", "true")

import pytest
from fastapi.testclient import TestClient

from zdrovena.api.main import app
from zdrovena.common.shipping_store import ShippingStore

RESET = "/api/__test__/shipping/reset"
DRAFTS = "/api/__test__/shipping/drafts"
DLQ = "/api/__test__/shipping/dlq"


@pytest.fixture()
def store(tmp_path) -> ShippingStore:
    return ShippingStore(local_root=tmp_path / "shipping")


@pytest.fixture()
def client(store):
    with patch("zdrovena.api.deps._shipping_store_singleton", return_value=store):
        with TestClient(app, raise_server_exceptions=True) as c:
            yield c


@pytest.fixture()
def enabled(monkeypatch):
    monkeypatch.setenv("PROVIDER_MODE", "fake")
    monkeypatch.setenv("APP_ENV", "staging")


def _draft(draft_id: str, **extra) -> dict:
    return {"id": draft_id, "status": "draft", **extra}


# ── Fail closed ──────────────────────────────────────────────────────────────


class TestDisabled:
    @pytest.mark.parametrize(
        ("provider_mode", "app_env"),
        [("", "staging"), ("real", "staging"), ("fake", "production")],
    )
    @pytest.mark.parametrize(
        ("path", "body"),
        [
            (RESET, None),
            (DRAFTS, {"id": "e2e-x"}),
            (DLQ, {"id": "e2e-x", "payload": {}}),
        ],
    )
    def test_returns_404_and_does_not_touch_store(
        self, client, store, monkeypatch, provider_mode, app_env, path, body
    ):
        monkeypatch.setenv("PROVIDER_MODE", provider_mode)
        monkeypatch.setenv("APP_ENV", app_env)
        store.upsert_draft(_draft("e2e-keep"))

        resp = client.post(path, json=body) if body is not None else client.post(path)

        assert resp.status_code == 404
        assert store.get_draft("e2e-keep") is not None
        assert store.list_dlq() == []


# ── Reset ────────────────────────────────────────────────────────────────────


class TestReset:
    def test_removes_only_e2e_records(self, client, store, enabled):
        store.upsert_draft(_draft("e2e-a"))
        store.upsert_draft(_draft("d-990", shopify_order_number="990123"))
        store.upsert_draft(_draft("real-1", shopify_order_number="1001"))
        store.enqueue_dlq(payload={"order_number": 990555}, error="x", entry_id="dlq-990")
        store.enqueue_dlq(payload={}, error="x", entry_id="e2e-dlq")
        store.enqueue_dlq(payload={"order_number": 1002}, error="x", entry_id="real-dlq")

        resp = client.post(RESET)

        assert resp.status_code == 200
        assert resp.json() == {"removed_drafts": 2, "removed_dlq": 2}
        assert store.get_draft("e2e-a") is None
        assert store.get_draft("d-990") is None
        assert store.get_draft("real-1") is not None
        assert store.get_dlq_entry("dlq-990") is None
        assert store.get_dlq_entry("e2e-dlq") is None
        assert store.get_dlq_entry("real-dlq") is not None

    def test_empty_store_reports_zero(self, client, enabled):
        resp = client.post(RESET)
        assert resp.status_code == 200
        assert resp.json() == {"removed_drafts": 0, "removed_dlq": 0}


# ── Seed draft ───────────────────────────────────────────────────────────────


class TestSeedDraft:
    def test_seeds_e2e_draft_by_id(self, client, store, enabled):
        resp = client.post(DRAFTS, json=_draft("e2e-seed", shopify_order_number="1"))

        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "e2e-seed"
        assert store.get_draft("e2e-seed") is not None

    def test_seeds_draft_with_990_order_number(self, client, store, enabled):
        resp = client.post(DRAFTS, json=_draft("d-1", order_number="990777"))

        assert resp.status_code == 200
        assert resp.json()["id"] == "d-1"
        assert store.get_draft("d-1") is not None

    def test_rejects_non_e2e_draft(self, client, store, enabled):
        resp = client.post(DRAFTS, json=_draft("real-1", shopify_order_number="1001"))

        assert resp.status_code == 400
        assert "e2e-" in resp.json()["detail"]
        assert store.get_draft("real-1") is None

    def test_rejects_missing_id(self, client, store, enabled):
        resp = client.post(DRAFTS, json={"order_number": "990001"})

        assert resp.status_code == 400
        assert resp.json()["detail"] == "Draft id required"
        assert store.list_drafts() == []

    def test_falls_back_to_body_when_store_returns_none(self, client, store, enabled):
        with patch.object(store, "get_draft", return_value=None):
            resp = client.post(DRAFTS, json=_draft("e2e-fallback"))

        assert resp.status_code == 200
        assert resp.json() == _draft("e2e-fallback")


# ── Seed DLQ ─────────────────────────────────────────────────────────────────


class TestSeedDlq:
    def test_defaults_error_and_source(self, client, store, enabled):
        resp = client.post(DLQ, json={"id": "e2e-dlq-1"})

        assert resp.status_code == 200
        entry = resp.json()
        assert entry["id"] == "e2e-dlq-1"
        assert entry["last_error"] == "E2E seeded failure"
        assert entry["source"] == "shopify"
        assert entry["payload"] == {}
        assert store.get_dlq_entry("e2e-dlq-1") is not None

    def test_generates_id_when_payload_marks_e2e(self, client, store, enabled):
        resp = client.post(
            DLQ,
            json={"payload": {"order_number": "990042"}, "error": "boom", "source": "allegro"},
        )

        assert resp.status_code == 200
        entry = resp.json()
        assert entry["id"]
        assert entry["last_error"] == "boom"
        assert entry["source"] == "allegro"
        assert store.get_dlq_entry(entry["id"]) is not None

    def test_event_falls_back_to_payload_id(self, client, enabled):
        with patch("zdrovena.api.routers.shipping.test_support.log_event") as event:
            resp = client.post(DLQ, json={"id": "e2e-dlq-2", "payload": {"id": 55}})

        assert resp.status_code == 200
        assert event.call_args.kwargs["order_number"] == 55

    def test_rejects_non_e2e_entry(self, client, store, enabled):
        with patch("zdrovena.api.routers.shipping.test_support.log_event") as event:
            resp = client.post(DLQ, json={"id": "real", "payload": {"order_number": 1001}})

        assert resp.status_code == 400
        assert "E2E DLQ id" in resp.json()["detail"]
        assert store.list_dlq() == []
        event.assert_not_called()
