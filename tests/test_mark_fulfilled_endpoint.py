"""Integration tests for POST /shipping/drafts/{id}/mark-fulfilled.

Covers the generic operator "mark as fulfilled" action:

- happy path for a non-Allegro draft (local-only status flip),
- happy path for an Allegro draft (also calls AllegroClient.mark_order_processed),
- idempotent second click (no repeated Allegro call),
- 404 when draft does not exist,
- 409 when an Allegro draft has no external_order_id,
- fulfilled_at (ISO UTC) and fulfilled_by (principal.email) are persisted.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
import responses as responses_lib
from fastapi.testclient import TestClient

os.environ.setdefault("AZURE_AUTH_DISABLED", "true")

from zdrovena.api.main import app
from zdrovena.common.shipping_store import ShippingStore
from zdrovena.common.storage import LocalStorageService

# ── fixtures ─────────────────────────────────────────────────────────────────


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
    """Persist a minimal draft directly through the store and return its id."""
    base = {
        "id": overrides.pop("id", "draft-test-1"),
        "source": "shopify",
        "status": "created",
        "courier": "inpost",
        "shopify_order_number": "#1001",
        "external_order_id": None,
    }
    base.update(overrides)
    store.upsert_draft(base)
    return base["id"]


# ── non-Allegro happy path ───────────────────────────────────────────────────


class TestMarkFulfilledNonAllegro:
    def test_local_only_flip_for_shopify_draft(self, client, store):
        draft_id = _make_draft(store, source="shopify")

        # We patch _get_allegro_client at the router level to make sure it is
        # NEVER invoked for a non-Allegro source.
        with patch("zdrovena.api.routers.webhooks._get_allegro_client") as get_client:
            resp = client.post(f"/api/shipping/drafts/{draft_id}/mark-fulfilled")

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "marked_fulfilled"
        assert body["draft_id"] == draft_id
        assert body["source"] == "shopify"
        assert body["allegro_side_effect"] is False
        assert body["fulfilled_at"]  # ISO string, non-empty
        assert body["fulfilled_by"] == "dev@localhost"

        # And the Allegro client factory was never even asked for a client.
        get_client.assert_not_called()

        # Persisted state
        draft = store.get_draft(draft_id)
        assert draft["fulfillment_status"] == "fulfilled"
        assert draft["fulfilled_at"] == body["fulfilled_at"]
        assert draft["fulfilled_by"] == "dev@localhost"
        # Non-Allegro drafts do NOT get the Allegro mirror fields.
        assert "allegro_fulfillment_status" not in draft
        assert "allegro_marked_processed_at" not in draft


# ── Allegro happy path ───────────────────────────────────────────────────────


class TestMarkFulfilledAllegro:
    def test_allegro_draft_calls_mark_order_processed(self, client, store):
        draft_id = _make_draft(
            store,
            id="draft-allegro-1",
            source="allegro",
            external_order_id="ORD-42",
            courier="allegro_delivery",
        )

        fake_client = MagicMock()
        with patch(
            "zdrovena.api.routers.webhooks._get_allegro_client",
            return_value=fake_client,
        ):
            resp = client.post(f"/api/shipping/drafts/{draft_id}/mark-fulfilled")

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "marked_fulfilled"
        assert body["source"] == "allegro"
        assert body["external_order_id"] == "ORD-42"
        assert body["allegro_side_effect"] is True
        assert body["fulfilled_by"] == "dev@localhost"

        # Allegro client was called exactly once with the external order id and SENT status.
        fake_client.mark_order_processed.assert_called_once_with("ORD-42", status="SENT")

        # Persisted state — both generic and Allegro mirror fields.
        draft = store.get_draft(draft_id)
        assert draft["fulfillment_status"] == "fulfilled"
        assert draft["fulfilled_at"] == body["fulfilled_at"]
        assert draft["fulfilled_by"] == "dev@localhost"
        assert draft["allegro_fulfillment_status"] == "SENT"
        assert draft["allegro_marked_processed_at"] == body["fulfilled_at"]
        assert draft["allegro_marked_processed_by"] == "dev@localhost"


# ── idempotency ──────────────────────────────────────────────────────────────


class TestMarkFulfilledIdempotency:
    def test_second_call_does_not_hit_allegro(self, client, store):
        draft_id = _make_draft(
            store,
            id="draft-allegro-idem",
            source="allegro",
            external_order_id="ORD-99",
            courier="allegro_delivery",
        )

        fake_client = MagicMock()
        with patch(
            "zdrovena.api.routers.webhooks._get_allegro_client",
            return_value=fake_client,
        ):
            first = client.post(f"/api/shipping/drafts/{draft_id}/mark-fulfilled")
            second = client.post(f"/api/shipping/drafts/{draft_id}/mark-fulfilled")

        assert first.status_code == 200
        assert first.json()["status"] == "marked_fulfilled"

        assert second.status_code == 200
        second_body = second.json()
        assert second_body["status"] == "already_fulfilled"
        assert second_body["allegro_side_effect"] is False
        # Timestamps preserved from the first call, not regenerated.
        assert second_body["fulfilled_at"] == first.json()["fulfilled_at"]
        assert second_body["fulfilled_by"] == first.json()["fulfilled_by"]

        # Allegro.mark_order_processed was called exactly ONCE across both calls.
        fake_client.mark_order_processed.assert_called_once_with("ORD-99", status="SENT")


# ── error paths ──────────────────────────────────────────────────────────────


class TestMarkFulfilledErrors:
    def test_404_when_draft_missing(self, client, store):
        resp = client.post("/api/shipping/drafts/does-not-exist/mark-fulfilled")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Draft not found"

    @pytest.mark.parametrize("state", ["cancelled", "error"])
    def test_409_when_cancelled_or_errored(self, client, store, state):
        # R5-A: a cancelled/errored draft was never shipped — refuse to fulfill.
        draft_id = _make_draft(store, id=f"draft-{state}", status=state)
        with patch("zdrovena.api.routers.webhooks._get_allegro_client") as get_client:
            resp = client.post(f"/api/shipping/drafts/{draft_id}/mark-fulfilled")
        assert resp.status_code == 409
        get_client.assert_not_called()
        assert store.get_draft(draft_id).get("fulfillment_status") != "fulfilled"

    def test_409_when_allegro_draft_missing_external_id(self, client, store):
        draft_id = _make_draft(
            store,
            id="draft-allegro-noext",
            source="allegro",
            external_order_id=None,
            courier="allegro_delivery",
        )
        # Even without the external id we should NOT touch Allegro.
        with patch("zdrovena.api.routers.webhooks._get_allegro_client") as get_client:
            resp = client.post(f"/api/shipping/drafts/{draft_id}/mark-fulfilled")

        assert resp.status_code == 409
        assert "external order id" in resp.json()["detail"].lower()
        get_client.assert_not_called()

        # Draft was NOT flipped to fulfilled.
        draft = store.get_draft(draft_id)
        assert draft.get("fulfillment_status") != "fulfilled"

    def test_allegro_api_error_bubbles_up_as_502(self, client, store):
        from zdrovena.common.shipping_exceptions import AllegroBusinessError

        draft_id = _make_draft(
            store,
            id="draft-allegro-502",
            source="allegro",
            external_order_id="ORD-BOOM",
            courier="allegro_delivery",
        )

        fake_client = MagicMock()
        fake_client.mark_order_processed.side_effect = AllegroBusinessError("order already SENT")
        with patch(
            "zdrovena.api.routers.webhooks._get_allegro_client",
            return_value=fake_client,
        ):
            resp = client.post(f"/api/shipping/drafts/{draft_id}/mark-fulfilled")

        assert resp.status_code == 502
        assert "Allegro API error" in resp.json()["detail"]

        # Local status must NOT flip if the external side-effect failed.
        draft = store.get_draft(draft_id)
        assert draft.get("fulfillment_status") != "fulfilled"


# ── Shopify fulfillment sync ─────────────────────────────────────────────────


class TestMarkFulfilledShopify:
    """mark-fulfilled for source=shopify calls Shopify Fulfillment API."""

    def _shopify_draft(self, store, tracking_number="123456789"):
        return _make_draft(
            store,
            id="draft-shopify-ff-1",
            source="shopify",
            external_order_id="4567890123",
            courier="inpost",
            tracking_number=tracking_number,
        )

    @responses_lib.activate
    def test_shopify_fulfillment_created_with_tracking(self, client, store):
        draft_id = self._shopify_draft(store)

        responses_lib.add(
            responses_lib.GET,
            "https://myshop.myshopify.com/admin/api/2024-01/orders/4567890123/fulfillment_orders.json",
            json={"fulfillment_orders": [{"id": 99, "status": "open"}]},
            status=200,
        )
        responses_lib.add(
            responses_lib.POST,
            "https://myshop.myshopify.com/admin/api/2024-01/fulfillments.json",
            json={"fulfillment": {"id": 777, "status": "success"}},
            status=201,
        )

        with (
            patch("zdrovena.api.routers.webhooks.get_secret", return_value="tok"),
            patch(
                "zdrovena.api.routers.webhooks._allowed_shopify_domains",
                return_value=frozenset(["myshop.myshopify.com"]),
            ),
        ):
            resp = client.post(f"/api/shipping/drafts/{draft_id}/mark-fulfilled")

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "marked_fulfilled"
        se = body["shopify_side_effect"]
        assert se["created"] is True
        assert se["shopify_fulfillment_id"] == "777"
        assert se["tracking_number"] == "123456789"

        # Verify fulfillment POST included tracking info + correct fulfillment_order_id
        posted = responses_lib.calls[1].request
        import json

        payload = json.loads(posted.body)
        assert (
            payload["fulfillment"]["line_items_by_fulfillment_order"][0]["fulfillment_order_id"]
            == 99
        )
        assert payload["fulfillment"]["tracking_info"]["number"] == "123456789"
        assert payload["fulfillment"]["tracking_info"]["company"] == "InPost"
        assert "sledzenie" in payload["fulfillment"]["tracking_info"]["url"]

    @responses_lib.activate
    def test_shopify_no_open_fulfillment_orders_returns_skipped(self, client, store):
        draft_id = self._shopify_draft(store)

        responses_lib.add(
            responses_lib.GET,
            "https://myshop.myshopify.com/admin/api/2024-01/orders/4567890123/fulfillment_orders.json",
            json={"fulfillment_orders": [{"id": 99, "status": "closed"}]},
            status=200,
        )

        with (
            patch("zdrovena.api.routers.webhooks.get_secret", return_value="tok"),
            patch(
                "zdrovena.api.routers.webhooks._allowed_shopify_domains",
                return_value=frozenset(["myshop.myshopify.com"]),
            ),
        ):
            resp = client.post(f"/api/shipping/drafts/{draft_id}/mark-fulfilled")

        assert resp.status_code == 200
        se = resp.json()["shopify_side_effect"]
        assert se == {"skipped": "no_open_fulfillment_orders"}

    def test_shopify_not_configured_returns_skipped(self, client, store):
        draft_id = self._shopify_draft(store)

        with patch("zdrovena.api.routers.webhooks.get_secret", return_value=None):
            resp = client.post(f"/api/shipping/drafts/{draft_id}/mark-fulfilled")

        assert resp.status_code == 200
        se = resp.json()["shopify_side_effect"]
        assert se == {"skipped": "shopify_not_configured"}
        # Draft still marked fulfilled locally
        assert store.get_draft(draft_id)["fulfillment_status"] == "fulfilled"

    @responses_lib.activate
    def test_shopify_api_error_does_not_block_local_fulfillment(self, client, store):
        draft_id = self._shopify_draft(store)

        responses_lib.add(
            responses_lib.GET,
            "https://myshop.myshopify.com/admin/api/2024-01/orders/4567890123/fulfillment_orders.json",
            json={"errors": "Not Found"},
            status=404,
        )

        with (
            patch("zdrovena.api.routers.webhooks.get_secret", return_value="tok"),
            patch(
                "zdrovena.api.routers.webhooks._allowed_shopify_domains",
                return_value=frozenset(["myshop.myshopify.com"]),
            ),
        ):
            resp = client.post(f"/api/shipping/drafts/{draft_id}/mark-fulfilled")

        assert resp.status_code == 200
        se = resp.json()["shopify_side_effect"]
        assert "error" in se
        # Draft IS fulfilled locally even though Shopify call failed
        assert store.get_draft(draft_id)["fulfillment_status"] == "fulfilled"
