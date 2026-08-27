"""Tests for zdrovena.api.routers.apaczka_pickup_poller.

Apaczka assigns a pickup number after order_send returns, so a shipment created
today can be missing the id the operator needs for a support ticket tomorrow.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from zdrovena.api.routers.apaczka_pickup_poller import (
    MAX_PICKUP_NUMBER_ATTEMPTS,
    resolve_apaczka_pickup_numbers_once,
)


def _draft(draft_id="d1", pickup_number="", attempts=0):
    return {
        "id": draft_id,
        "shopify_order_number": "1801",
        "courier": "apaczka",
        "apaczka_service_id": "21",
        "status": "created",
        "pickup_ordered": True,
        "pickup_number_attempts": attempts,
        "courier_shipments": [
            {
                "id": "ord-1",
                "tracking_number": "APZ1",
                "package_type": "1-pak",
                "package_number": "1",
                "pickup_number": pickup_number,
            }
        ],
    }


class TestResolveApaczkaPickupNumbersOnce:
    def test_fills_in_a_missing_number(self):
        store = MagicMock()
        store.list_drafts.return_value = [_draft()]
        client = MagicMock()
        client.get_order_pickup_number.return_value = "ZO-77123"

        stats = resolve_apaczka_pickup_numbers_once(shipping_store=store, client=client)

        assert stats == {"scanned": 1, "resolved": 1, "still_pending": 0, "errors": 0}
        written = store.update_draft.call_args.args[1]
        assert written["courier_shipments"][0]["pickup_number"] == "ZO-77123"

    def test_skips_drafts_that_already_have_every_number(self):
        store = MagicMock()
        store.list_drafts.return_value = [_draft(pickup_number="ZO-1")]
        client = MagicMock()

        stats = resolve_apaczka_pickup_numbers_once(shipping_store=store, client=client)

        assert stats["scanned"] == 0
        client.get_order_pickup_number.assert_not_called()

    def test_counts_a_still_empty_number_as_pending_and_records_the_attempt(self):
        store = MagicMock()
        store.list_drafts.return_value = [_draft()]
        client = MagicMock()
        client.get_order_pickup_number.return_value = ""

        stats = resolve_apaczka_pickup_numbers_once(shipping_store=store, client=client)

        assert stats["still_pending"] == 1
        assert store.update_draft.call_args.args[1]["pickup_number_attempts"] == 1

    def test_gives_up_after_the_attempt_cap(self):
        # An order the carrier never numbers must not be retried on every cycle
        # for the rest of its life.
        store = MagicMock()
        store.list_drafts.return_value = [_draft(attempts=MAX_PICKUP_NUMBER_ATTEMPTS)]
        client = MagicMock()

        stats = resolve_apaczka_pickup_numbers_once(shipping_store=store, client=client)

        assert stats["scanned"] == 0
        client.get_order_pickup_number.assert_not_called()

    def test_one_bad_draft_does_not_stop_the_rest(self):
        store = MagicMock()
        store.list_drafts.return_value = [_draft("d1"), _draft("d2")]
        client = MagicMock()
        client.get_order_pickup_number.side_effect = [RuntimeError("boom"), "ZO-2"]

        stats = resolve_apaczka_pickup_numbers_once(shipping_store=store, client=client)

        assert stats["errors"] == 1
        assert stats["resolved"] == 1

    def test_a_store_read_failure_is_reported_not_raised(self):
        store = MagicMock()
        store.list_drafts.side_effect = RuntimeError("table offline")

        stats = resolve_apaczka_pickup_numbers_once(shipping_store=store, client=MagicMock())

        assert stats["errors"] == 1

    def test_ignores_drafts_from_other_couriers(self):
        store = MagicMock()
        inpost = _draft("d-inpost")
        inpost["courier"] = "inpost"
        store.list_drafts.return_value = [inpost]
        client = MagicMock()

        stats = resolve_apaczka_pickup_numbers_once(shipping_store=store, client=client)

        assert stats["scanned"] == 0
        client.get_order_pickup_number.assert_not_called()

    def test_mock_courier_skips_the_cycle(self):
        store = MagicMock()
        with patch.dict("os.environ", {"MOCK_COURIER": "1"}):
            stats = resolve_apaczka_pickup_numbers_once(shipping_store=store, client=MagicMock())
        assert stats == {"scanned": 0, "resolved": 0, "still_pending": 0, "errors": 0}
        store.list_drafts.assert_not_called()
