"""Automatic Shopify fulfillment when a draft gets its tracking number (#541).

Runs against :mod:`tests.fake_shopify`, which closes fulfillment-order lines as
they are fulfilled and counts one customer email per notifying fulfillment.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
import responses
from fastapi.testclient import TestClient

os.environ.setdefault("AZURE_AUTH_DISABLED", "true")

from tests.fake_shopify import DEPOSIT, WATER, fake_shopify
from zdrovena.api import shipping_execution_composition as execution_composition
from zdrovena.api.main import app
from zdrovena.api.shopify_fulfillment_composition import (
    AUTO_FULFILL_ACTOR,
    AUTO_FULFILL_ENV,
    maybe_auto_fulfill,
    retry_shopify_auto_fulfillments_once,
)
from zdrovena.common.shipping_store import ShippingStore
from zdrovena.common.storage import LocalStorageService

ORDER_ID = "4567890123"


@pytest.fixture()
def store(tmp_path) -> ShippingStore:
    return ShippingStore(local_root=tmp_path / "store")


@pytest.fixture()
def flag_on(monkeypatch):
    monkeypatch.setenv(AUTO_FULFILL_ENV, "true")


@pytest.fixture()
def flag_off(monkeypatch):
    monkeypatch.delenv(AUTO_FULFILL_ENV, raising=False)


@pytest.fixture()
def client(store, tmp_path):
    storage = LocalStorageService(root=tmp_path / "storage")
    with patch("zdrovena.api.deps._storage_singleton", return_value=storage):
        with patch("zdrovena.api.deps._shipping_store_singleton", return_value=store):
            with TestClient(app, raise_server_exceptions=True) as c:
                yield c


def _draft(store: ShippingStore, **overrides) -> str:
    record = {
        "id": "draft-auto-1",
        "source": "shopify",
        "status": "created",
        "courier": "inpost",
        "shopify_order_number": "#1001",
        "external_order_id": ORDER_ID,
        "tracking_number": "620111222333",
        **overrides,
    }
    store.upsert_draft(record)
    return record["id"]


class TestAutoFulfill:
    def test_water_and_deposit(self, store, flag_on):
        draft_id = _draft(store)
        with fake_shopify(ORDER_ID, [WATER, DEPOSIT]) as shop:
            result = maybe_auto_fulfill(draft_id, store)

        assert result["created"] is True
        assert len(shop.fulfillments) == 2
        water, deposit = shop.fulfillments
        assert water["notify_customer"] is True
        assert water["tracking_info"] == {
            "number": "620111222333",
            "company": "InPost",
            "url": "https://inpost.pl/sledzenie-przesylek?number=620111222333",
        }
        assert deposit["notify_customer"] is False
        assert "tracking_info" not in deposit
        # Exactly one shipping email, listing only the water.
        assert shop.emails == [{"items": [WATER["title"]], "tracking_info": water["tracking_info"]}]
        assert shop.order_fulfilled

        draft = store.get_draft(draft_id)
        assert draft["fulfillment_status"] == "fulfilled"
        assert draft["fulfilled_at"]
        assert draft["fulfilled_by"] == AUTO_FULFILL_ACTOR
        assert draft["shopify_fulfillment_ids"] == ["777", "778"]
        assert not draft.get("shopify_fulfillment_error")

    def test_only_water(self, store, flag_on):
        draft_id = _draft(store, courier="apaczka", apaczka_service_id="21", tracking_number="DPD1")
        with fake_shopify(ORDER_ID, [WATER]) as shop:
            maybe_auto_fulfill(draft_id, store)

        assert len(shop.fulfillments) == 1
        assert shop.fulfillments[0]["notify_customer"] is True
        assert shop.fulfillments[0]["tracking_info"]["company"] == "DPD"
        assert len(shop.emails) == 1
        assert shop.order_fulfilled
        assert store.get_draft(draft_id)["fulfillment_status"] == "fulfilled"

    def test_only_deposit(self, store, flag_on):
        draft_id = _draft(store)
        with fake_shopify(ORDER_ID, [DEPOSIT]) as shop:
            maybe_auto_fulfill(draft_id, store)

        assert len(shop.fulfillments) == 1
        assert shop.fulfillments[0]["notify_customer"] is False
        assert shop.emails == []
        assert shop.order_fulfilled
        assert store.get_draft(draft_id)["fulfillment_status"] == "fulfilled"

    def test_repeated_trigger_creates_nothing_new(self, store, flag_on):
        draft_id = _draft(store)
        with fake_shopify(ORDER_ID, [WATER, DEPOSIT]) as shop:
            maybe_auto_fulfill(draft_id, store)
            second = maybe_auto_fulfill(draft_id, store)
            third = retry_shopify_auto_fulfillments_once(shipping_store=store)

        assert second is None
        assert third["scanned"] == 0
        assert len(shop.fulfillments) == 2
        assert len(shop.emails) == 1

    def test_lost_ids_still_cannot_double_fulfill(self, store, flag_on):
        """Even if the ids were never stored, Shopify's closed lines stop a repeat."""
        draft_id = _draft(store)
        with fake_shopify(ORDER_ID, [WATER, DEPOSIT]) as shop:
            maybe_auto_fulfill(draft_id, store)
            store.update_draft(
                draft_id, {"shopify_fulfillment_ids": [], "fulfillment_status": None}
            )
            result = maybe_auto_fulfill(draft_id, store)

        assert result == {"skipped": "no_open_fulfillment_orders"}
        assert len(shop.fulfillments) == 2

    def test_manual_action_after_automatic_one(self, store, flag_on, client):
        draft_id = _draft(store)
        with fake_shopify(ORDER_ID, [WATER, DEPOSIT]) as shop:
            maybe_auto_fulfill(draft_id, store)
            resp = client.post(f"/api/shipping/drafts/{draft_id}/mark-fulfilled")

        assert resp.status_code == 200
        assert resp.json()["status"] == "already_fulfilled"
        assert len(shop.fulfillments) == 2

    def test_manual_action_reuses_stored_ids(self, store, flag_on, client):
        """Ids on a draft that is not (yet) locally fulfilled are reused, not re-created."""
        draft_id = _draft(store)
        with fake_shopify(ORDER_ID, [WATER, DEPOSIT]) as shop:
            maybe_auto_fulfill(draft_id, store)
            store.update_draft(draft_id, {"fulfillment_status": None})
            resp = client.post(f"/api/shipping/drafts/{draft_id}/mark-fulfilled")

        body = resp.json()
        assert body["status"] == "marked_fulfilled"
        assert body["shopify_side_effect"]["reused"] is True
        assert body["shopify_side_effect"]["shopify_fulfillment_ids"] == ["777", "778"]
        assert len(shop.fulfillments) == 2

    def test_shopify_error_is_stored_and_retried(self, store, flag_on):
        draft_id = _draft(store)
        with fake_shopify(ORDER_ID, [WATER, DEPOSIT]) as shop:
            shop.fail_fulfillment_orders_get = 503
            result = maybe_auto_fulfill(draft_id, store)

            draft = store.get_draft(draft_id)
            assert "error" in result
            assert "503" in draft["shopify_fulfillment_error"]
            assert draft.get("fulfillment_status") != "fulfilled"
            assert draft["status"] == "created"  # the label is untouched

            shop.fail_fulfillment_orders_get = None
            stats = retry_shopify_auto_fulfillments_once(shipping_store=store)

        assert stats == {"scanned": 1, "fulfilled": 1, "errors": 0}
        draft = store.get_draft(draft_id)
        assert draft["fulfillment_status"] == "fulfilled"
        assert draft["shopify_fulfillment_error"] is None
        assert len(shop.emails) == 1

    def test_partial_failure_retries_only_the_rest(self, store, flag_on):
        draft_id = _draft(store)
        with fake_shopify(ORDER_ID, [WATER, DEPOSIT]) as shop:
            # Water succeeds, deposit fails.
            original = shop._post_fulfillment
            calls = {"n": 0}

            def flaky(request):
                calls["n"] += 1
                if calls["n"] == 2:
                    return (500, {}, '{"errors": "down"}')
                return original(request)

            shop._post_fulfillment = flaky
            with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
                shop.register(mock)
                maybe_auto_fulfill(draft_id, store)
                draft = store.get_draft(draft_id)
                assert draft["shopify_fulfillment_ids"] == ["777"]
                assert draft["shopify_fulfillment_error"]

                retry_shopify_auto_fulfillments_once(shipping_store=store)

        draft = store.get_draft(draft_id)
        assert draft["shopify_fulfillment_ids"] == ["777", "778"]
        assert draft["fulfillment_status"] == "fulfilled"
        assert len(shop.fulfillments) == 2
        assert len(shop.emails) == 1
        assert shop.order_fulfilled

    def test_never_raises(self, flag_on):
        store = MagicMock()
        store.get_draft.side_effect = RuntimeError("store down")
        assert maybe_auto_fulfill("x", store) == {"error": "auto_fulfill_failed"}

    @pytest.mark.parametrize(
        "overrides",
        [
            {"source": "allegro"},
            {"tracking_number": ""},
            {"status": "pending_confirmation"},
            {"status": "cancelled"},
            {"fulfillment_status": "fulfilled"},
        ],
    )
    def test_ineligible_drafts_are_left_alone(self, store, flag_on, overrides):
        draft_id = _draft(store, **overrides)
        with fake_shopify(ORDER_ID, [WATER]) as shop:
            assert maybe_auto_fulfill(draft_id, store) is None
        assert shop.fulfillments == []


class TestFlagOff:
    def test_nothing_happens_automatically(self, store, flag_off):
        draft_id = _draft(store)
        with fake_shopify(ORDER_ID, [WATER, DEPOSIT]) as shop:
            assert maybe_auto_fulfill(draft_id, store) is None
            store.update_draft(draft_id, {"shopify_fulfillment_error": "old"})
            assert retry_shopify_auto_fulfillments_once(shipping_store=store)["scanned"] == 0

        assert shop.fulfillments == []
        assert store.get_draft(draft_id).get("fulfillment_status") != "fulfilled"

    def test_manual_path_still_splits_lines(self, store, flag_off, client):
        draft_id = _draft(store)
        with fake_shopify(ORDER_ID, [WATER, DEPOSIT]) as shop:
            resp = client.post(f"/api/shipping/drafts/{draft_id}/mark-fulfilled")

        assert resp.status_code == 200
        assert [f["notify_customer"] for f in shop.fulfillments] == [True, False]
        assert len(shop.emails) == 1
        assert store.get_draft(draft_id)["shopify_fulfillment_ids"] == ["777", "778"]


class TestTriggers:
    def test_execution_of_a_synchronous_carrier(self, store, flag_on):
        draft_id = _draft(store, status="pending", tracking_number=None)

        def fake_execute(draft_id_arg, repository, **_kwargs):
            repository.update_draft(draft_id_arg, {"status": "created", "tracking_number": "APZ1"})
            return repository.get_draft(draft_id_arg)

        with (
            fake_shopify(ORDER_ID, [WATER, DEPOSIT]) as shop,
            patch.object(execution_composition.execution_workflow, "execute_draft", fake_execute),
        ):
            result = execution_composition.execute_shipping_draft(draft_id, store, storage=None)

        assert result["fulfillment_status"] == "fulfilled"
        assert len(shop.emails) == 1

    def test_execution_failure_in_shopify_does_not_block_labels(self, store, flag_on):
        draft_id = _draft(store, status="pending", tracking_number=None)

        def fake_execute(draft_id_arg, repository, **_kwargs):
            repository.update_draft(draft_id_arg, {"status": "created", "tracking_number": "APZ1"})
            return repository.get_draft(draft_id_arg)

        with (
            fake_shopify(ORDER_ID, [WATER]) as shop,
            patch.object(execution_composition.execution_workflow, "execute_draft", fake_execute),
        ):
            shop.fail_fulfillment_orders_get = 500
            result = execution_composition.execute_shipping_draft(draft_id, store, storage=None)

        assert result["status"] == "created"
        assert result["tracking_number"] == "APZ1"
        assert result["shopify_fulfillment_error"]

    def test_inpost_poller_resolution(self, store, flag_on, monkeypatch):
        from zdrovena.api.routers.inpost_poller import resolve_pending_inpost_once

        monkeypatch.delenv("MOCK_COURIER", raising=False)
        draft_id = _draft(
            store,
            status="pending_confirmation",
            tracking_number=None,
            courier_draft_id="ship-1",
            courier_shipments=[
                {
                    "id": "ship-1",
                    "tracking_number": "",
                    "package_type": "1-pak",
                    "package_number": "1",
                }
            ],
        )
        inpost = MagicMock()
        inpost.get_shipment.return_value = {"id": "ship-1", "tracking_number": "620DONE"}

        with fake_shopify(ORDER_ID, [WATER, DEPOSIT]) as shop:
            stats = resolve_pending_inpost_once(shipping_store=store, client=inpost)

        assert stats["resolved"] == 1
        assert shop.fulfillments[0]["tracking_info"]["number"] == "620DONE"
        assert len(shop.emails) == 1
        assert store.get_draft(draft_id)["fulfillment_status"] == "fulfilled"

    def test_inpost_poller_with_flag_off_does_not_call_shopify(self, store, flag_off, monkeypatch):
        from zdrovena.api.routers.inpost_poller import resolve_pending_inpost_once

        monkeypatch.delenv("MOCK_COURIER", raising=False)
        draft_id = _draft(
            store,
            status="pending_confirmation",
            tracking_number=None,
            courier_draft_id="ship-1",
            courier_shipments=[
                {
                    "id": "ship-1",
                    "tracking_number": "",
                    "package_type": "1-pak",
                    "package_number": "1",
                }
            ],
        )
        inpost = MagicMock()
        inpost.get_shipment.return_value = {"id": "ship-1", "tracking_number": "620DONE"}

        with fake_shopify(ORDER_ID, [WATER]) as shop:
            resolve_pending_inpost_once(shipping_store=store, client=inpost)

        assert shop.fulfillments == []
        assert store.get_draft(draft_id).get("fulfillment_status") != "fulfilled"
