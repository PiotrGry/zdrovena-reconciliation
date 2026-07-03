"""Tests for POST /api/shipping/drafts/{id}/confirm — pending_confirmation worker (P1-3).

Ship-with-Allegro create-commands are asynchronous. execute_draft returns
`pending_confirmation` when the command is still IN_PROGRESS after the short
in-request polling window. This endpoint is the durable follow-up.

All tests run with AZURE_AUTH_DISABLED=true (dev principal has admin role).
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

os.environ.setdefault("AZURE_AUTH_DISABLED", "true")

import pytest
from fastapi.testclient import TestClient

from zdrovena.api.main import app
from zdrovena.common.shipping_exceptions import AllegroAuthError
from zdrovena.common.shipping_store import ShippingStore


@pytest.fixture()
def store(tmp_path) -> ShippingStore:
    return ShippingStore(local_root=tmp_path / "shipping")


@pytest.fixture()
def client(store):
    with patch("zdrovena.api.deps._shipping_store_singleton", return_value=store):
        with TestClient(app, raise_server_exceptions=True) as c:
            yield c


def _seed_pending_draft(store: ShippingStore, **overrides: object) -> str:
    """Seed a draft in pending_confirmation state with an outstanding command_id."""
    record: dict = {
        "id": "draft-pending-1",
        "shopify_order_id": "111",
        "shopify_order_number": "SO-1",
        "external_order_id": "ORD-1",
        "courier": "allegro_delivery",
        "status": "pending_confirmation",
        "allegro_command_id": "cmd-async-1",
        "receiver": {},
        "shipping_lines": [],
    }
    record.update(overrides)
    store.upsert_draft(record)
    return record["id"]


class TestConfirmPendingCommand:
    def test_success_updates_draft_and_returns_created(self, client, store):
        draft_id = _seed_pending_draft(store)

        allegro_client = MagicMock()
        allegro_client.get_ship_with_allegro_command_status.return_value = {
            "status": "SUCCESS",
            "shipmentId": "ship-42",
        }
        allegro_client.get_ship_with_allegro_shipment.return_value = {
            "packages": [{"transportingInfo": [{"carrierId": "INPOST", "carrierWaybill": "W1"}]}]
        }
        allegro_client.extract_shipment_waybill = MagicMock(return_value=("INPOST", "W1"))

        with patch(
            "zdrovena.api.routers.webhooks._get_allegro_client",
            return_value=allegro_client,
        ):
            resp = client.post(f"/api/shipping/drafts/{draft_id}/confirm")

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "created"
        assert body["courier_draft_id"] == "ship-42"
        assert body["tracking_number"] == "W1"

        # Persistence
        loaded = store.get_draft(draft_id)
        assert loaded["status"] == "created"
        assert loaded["allegro_shipment_id"] == "ship-42"

    def test_still_in_progress_returns_202(self, client, store):
        draft_id = _seed_pending_draft(store)

        allegro_client = MagicMock()
        allegro_client.get_ship_with_allegro_command_status.return_value = {
            "status": "IN_PROGRESS"
        }

        with patch(
            "zdrovena.api.routers.webhooks._get_allegro_client",
            return_value=allegro_client,
        ):
            resp = client.post(f"/api/shipping/drafts/{draft_id}/confirm")

        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "pending_confirmation"
        assert body["allegro_command_id"] == "cmd-async-1"

        # Draft must remain in pending_confirmation (no state mutation)
        loaded = store.get_draft(draft_id)
        assert loaded["status"] == "pending_confirmation"

    def test_error_status_writes_error_and_502(self, client, store):
        draft_id = _seed_pending_draft(store)

        allegro_client = MagicMock()
        allegro_client.get_ship_with_allegro_command_status.return_value = {
            "status": "ERROR",
            "errors": [{"message": "Invalid receiver postal code"}],
        }

        with patch(
            "zdrovena.api.routers.webhooks._get_allegro_client",
            return_value=allegro_client,
        ):
            resp = client.post(f"/api/shipping/drafts/{draft_id}/confirm")

        assert resp.status_code == 502
        loaded = store.get_draft(draft_id)
        assert loaded["status"] == "error"
        assert "Invalid receiver postal code" in loaded["error"]

    def test_wrong_state_returns_409(self, client, store):
        # Draft is 'created' — not eligible for confirm
        store.upsert_draft(
            {
                "id": "draft-x",
                "courier": "allegro_delivery",
                "status": "created",
                "allegro_command_id": "cmd-x",
                "shopify_order_id": "1",
                "shopify_order_number": "X",
                "external_order_id": "OX",
                "receiver": {},
                "shipping_lines": [],
            }
        )
        resp = client.post("/api/shipping/drafts/draft-x/confirm")
        assert resp.status_code == 409

    def test_missing_command_id_returns_409(self, client, store):
        draft_id = _seed_pending_draft(store, allegro_command_id=None)
        resp = client.post(f"/api/shipping/drafts/{draft_id}/confirm")
        assert resp.status_code == 409

    def test_draft_not_found_returns_404(self, client, store):
        resp = client.post("/api/shipping/drafts/nope/confirm")
        assert resp.status_code == 404

    def test_transient_error_returns_502_no_state_change(self, client, store):
        draft_id = _seed_pending_draft(store)

        allegro_client = MagicMock()
        allegro_client.get_ship_with_allegro_command_status.side_effect = AllegroAuthError(
            "token expired"
        )

        with patch(
            "zdrovena.api.routers.webhooks._get_allegro_client",
            return_value=allegro_client,
        ):
            resp = client.post(f"/api/shipping/drafts/{draft_id}/confirm")

        assert resp.status_code == 502
        # Draft must not have been mutated on transient failure
        loaded = store.get_draft(draft_id)
        assert loaded["status"] == "pending_confirmation"

    def test_idempotent_repeated_confirm_after_success(self, client, store):
        """Calling confirm twice should just return 409 the second time (already created)."""
        draft_id = _seed_pending_draft(store)
        allegro_client = MagicMock()
        allegro_client.get_ship_with_allegro_command_status.return_value = {
            "status": "SUCCESS",
            "shipmentId": "ship-99",
        }
        allegro_client.get_ship_with_allegro_shipment.return_value = {
            "packages": [{"transportingInfo": [{"carrierId": "INPOST", "carrierWaybill": "W"}]}]
        }
        allegro_client.extract_shipment_waybill = MagicMock(return_value=("INPOST", "W"))

        with patch(
            "zdrovena.api.routers.webhooks._get_allegro_client",
            return_value=allegro_client,
        ):
            first = client.post(f"/api/shipping/drafts/{draft_id}/confirm")
            second = client.post(f"/api/shipping/drafts/{draft_id}/confirm")

        assert first.status_code == 200
        assert second.status_code == 409  # no longer pending_confirmation
