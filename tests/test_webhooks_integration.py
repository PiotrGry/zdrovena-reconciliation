"""End-to-end integration: webhook → real ShippingStore → execute → label.

These tests use a real on-disk ShippingStore (no mocking the store layer) and
exercise the full draft lifecycle. Only the courier HTTP clients are stubbed —
everything else is the production code path.

Audit reference: zdrovena_test_audit.md §7.5 — integration coverage is thin;
the existing tests mock the store, which hides serialization/persistence bugs.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("AZURE_AUTH_DISABLED", "true")

from zdrovena.api.main import app
from zdrovena.common.shipping_store import ShippingStore
from zdrovena.common.storage import LocalStorageService

_FIXTURES = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> dict:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def _sign_shopify(body: bytes, secret: str) -> str:
    return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


@pytest.fixture(autouse=True)
def _allow_unsigned_shopify_webhooks(monkeypatch):
    """Opt these integration tests out of the fail-closed unsigned-webhook policy.

    Production behaviour: when ``_get_webhook_secret()`` returns None and the
    ``ALLOW_UNSIGNED_SHOPIFY_WEBHOOKS`` flag is not set, the webhook endpoint
    returns 503. These tests focus on the post-HMAC pipeline (store → execute
    → label), so we set the flag here and selectively patch the secret in
    individual tests when verifying HMAC behaviour itself.
    """
    monkeypatch.setenv("ALLOW_UNSIGNED_SHOPIFY_WEBHOOKS", "true")


@pytest.fixture()
def store(tmp_path) -> ShippingStore:
    return ShippingStore(local_root=tmp_path / "store")


@pytest.fixture()
def storage(tmp_path) -> LocalStorageService:
    return LocalStorageService(root=tmp_path / "storage")


@pytest.fixture()
def client(store, storage):
    """TestClient wired to the real ShippingStore + LocalStorageService."""
    with patch("zdrovena.api.deps._storage_singleton", return_value=storage):
        with patch("zdrovena.api.deps._shipping_store_singleton", return_value=store):
            with TestClient(app, raise_server_exceptions=True) as c:
                yield c


# ── Flow: InPost kurier — webhook → list → execute → label ────────────────────


class TestInPostKurierFullFlow:
    def test_webhook_creates_persistent_draft(self, client, store):
        order = _load_fixture("shopify_order_inpost_kurier.json")
        body = json.dumps(order).encode()
        with patch("zdrovena.api.routers.webhooks._get_webhook_secret", return_value=None):
            resp = client.post(
                "/api/webhooks/shopify/order-created",
                content=body,
                headers={"Content-Type": "application/json"},
            )
        assert resp.status_code == 200

        # Real store should contain exactly one draft now (background task ran)
        drafts = store.list_drafts()
        assert len(drafts) == 1
        d = drafts[0]
        assert d["courier"] == "inpost"
        assert d["status"] == "pending"
        assert d["shopify_order_id"] == str(order["id"])

    def test_execute_then_label_roundtrip(self, client, store):
        # Seed a pending draft directly via the real store
        order = _load_fixture("shopify_order_inpost_kurier.json")
        body = json.dumps(order).encode()
        with patch("zdrovena.api.routers.webhooks._get_webhook_secret", return_value=None):
            client.post(
                "/api/webhooks/shopify/order-created",
                content=body,
                headers={"Content-Type": "application/json"},
            )
        draft_id = store.list_drafts()[0]["id"]

        # Execute with the courier client stubbed at session level
        with patch("zdrovena.api.routers.webhooks.get_secret", return_value="tok"):
            with patch("zdrovena.common.inpost.InPostClient.create_kurier_shipment") as mock_ship:
                with patch("zdrovena.common.inpost.InPostClient.create_dispatch_order"):
                    mock_ship.return_value = {"id": "ship-99", "tracking_number": "TRK99"}
                    resp = client.post(f"/api/shipping/drafts/{draft_id}/execute")
        assert resp.status_code == 200

        # Draft survives in the real store with persisted patch
        loaded = store.get_draft(draft_id)
        assert loaded["status"] == "created"
        assert loaded["courier_draft_id"] == "ship-99"
        assert loaded["tracking_number"] == "TRK99"

        # Now fetch the label — InPostClient.get_label returns bytes
        fake_pdf = b"%PDF-1.4 inpost-label"
        with patch("zdrovena.api.routers.webhooks.get_secret", return_value="tok"):
            with patch("zdrovena.common.inpost.InPostClient.get_label", return_value=fake_pdf):
                resp = client.get(f"/api/shipping/drafts/{draft_id}/label")

        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.content == fake_pdf

    def test_execute_then_409_on_second_execute(self, client, store):
        order = _load_fixture("shopify_order_inpost_kurier.json")
        body = json.dumps(order).encode()
        with patch("zdrovena.api.routers.webhooks._get_webhook_secret", return_value=None):
            client.post(
                "/api/webhooks/shopify/order-created",
                content=body,
                headers={"Content-Type": "application/json"},
            )
        draft_id = store.list_drafts()[0]["id"]

        with patch("zdrovena.api.routers.webhooks.get_secret", return_value="tok"):
            with patch(
                "zdrovena.common.inpost.InPostClient.create_kurier_shipment",
                return_value={"id": "ship-1", "tracking_number": "T1"},
            ):
                with patch("zdrovena.common.inpost.InPostClient.create_dispatch_order"):
                    client.post(f"/api/shipping/drafts/{draft_id}/execute")
                    # Second execute on a created draft -> 409
                    resp2 = client.post(f"/api/shipping/drafts/{draft_id}/execute")
        assert resp2.status_code == 409


# ── HMAC end-to-end ───────────────────────────────────────────────────────────


class TestHmacEndToEnd:
    def test_valid_signature_creates_draft_via_real_store(self, client, store):
        order = _load_fixture("shopify_order_inpost_kurier.json")
        body = json.dumps(order).encode()
        secret = "live-secret-xyz"
        sig = _sign_shopify(body, secret)
        with patch("zdrovena.api.routers.webhooks._get_webhook_secret", return_value=secret):
            resp = client.post(
                "/api/webhooks/shopify/order-created",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Shopify-Hmac-Sha256": sig,
                },
            )
        assert resp.status_code == 200
        assert len(store.list_drafts()) == 1

    def test_invalid_signature_does_not_persist_draft(self, client, store):
        order = _load_fixture("shopify_order_inpost_kurier.json")
        body = json.dumps(order).encode()
        with patch("zdrovena.api.routers.webhooks._get_webhook_secret", return_value="real"):
            resp = client.post(
                "/api/webhooks/shopify/order-created",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Shopify-Hmac-Sha256": "fake-sig",
                },
            )
        assert resp.status_code == 401
        # No draft should have been written to the real store
        assert store.list_drafts() == []


# ── TDD-red: idempotent webhook by shopify_order_id ──────────────────────────


class TestWebhookIdempotency:
    """**TDD-red** — Shopify retries webhooks on timeouts. Same order arriving
    twice produces two drafts because _create_draft generates a fresh UUID
    each time (webhooks.py:339). Target: dedup on shopify_order_id.

    Audit \u00a77.4 + audit \u00a77.5.
    """

    def test_duplicate_webhook_produces_single_draft(self, client, store):
        order = _load_fixture("shopify_order_inpost_kurier.json")
        body = json.dumps(order).encode()
        with patch("zdrovena.api.routers.webhooks._get_webhook_secret", return_value=None):
            client.post(
                "/api/webhooks/shopify/order-created",
                content=body,
                headers={"Content-Type": "application/json"},
            )
            # Shopify resends the exact same payload
            client.post(
                "/api/webhooks/shopify/order-created",
                content=body,
                headers={"Content-Type": "application/json"},
            )

        drafts = [d for d in store.list_drafts() if d["shopify_order_id"] == str(order["id"])]
        assert len(drafts) == 1, f"Expected single draft for repeated webhook, got {len(drafts)}"


# ── Error paths against the real store ───────────────────────────────────────


class TestExecuteFailurePersistsErrorOnStore:
    def test_courier_exception_writes_error_field_to_real_store(self, client, store):
        order = _load_fixture("shopify_order_inpost_kurier.json")
        body = json.dumps(order).encode()
        with patch("zdrovena.api.routers.webhooks._get_webhook_secret", return_value=None):
            client.post(
                "/api/webhooks/shopify/order-created",
                content=body,
                headers={"Content-Type": "application/json"},
            )
        draft_id = store.list_drafts()[0]["id"]

        with patch("zdrovena.api.routers.webhooks.get_secret", return_value="tok"):
            with patch(
                "zdrovena.common.inpost.InPostClient.create_kurier_shipment",
                side_effect=Exception("upstream-down"),
            ):
                resp = client.post(f"/api/shipping/drafts/{draft_id}/execute")
        assert resp.status_code == 502

        # The error must be persisted (not lost in memory)
        loaded = store.get_draft(draft_id)
        assert loaded["status"] == "error"
        assert "upstream-down" in (loaded.get("error") or "")

    def test_label_endpoint_404_when_no_courier_draft_id(self, client, store):
        order = _load_fixture("shopify_order_inpost_kurier.json")
        body = json.dumps(order).encode()
        with patch("zdrovena.api.routers.webhooks._get_webhook_secret", return_value=None):
            client.post(
                "/api/webhooks/shopify/order-created",
                content=body,
                headers={"Content-Type": "application/json"},
            )
        draft_id = store.list_drafts()[0]["id"]
        # Draft is "pending" with courier_draft_id=None — label must 404
        resp = client.get(f"/api/shipping/drafts/{draft_id}/label")
        assert resp.status_code == 404
