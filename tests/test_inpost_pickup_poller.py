"""Tests for zdrovena.api.routers.inpost_pickup_poller.

A pickup booked in the InPost app or InPost Manager never passed through the
portal, so the draft kept offering "Zamów podjazd" and invited a duplicate
courier visit (#552).
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

from zdrovena.api.routers.inpost_pickup_poller import (
    sync_inpost_pickup_for_draft,
    sync_inpost_pickups_once,
)
from zdrovena.common.inpost import InPostClient
from zdrovena.common.shipping_exceptions import InPostTransientError


def _created_draft(draft_id: str = "d1", shipment_id: str = "ship-1", **extra) -> dict:
    return {
        "id": draft_id,
        "shopify_order_number": "1700",
        "courier": "inpost",
        "service": "inpost_courier_standard",
        "status": "created",
        "courier_draft_id": shipment_id,
        "tracking_number": "620TRACK",
        "pickup_ordered": False,
        "dispatch_order_id": None,
        "courier_shipments": [
            {
                "id": shipment_id,
                "tracking_number": "620TRACK",
                "package_type": "1-pak",
                "package_number": "1",
            }
        ],
        **extra,
    }


def _dispatch(order_id, *shipment_ids, status="sent") -> dict:
    return {
        "id": order_id,
        "status": status,
        "shipments": [
            {"id": sid, "href": f"https://x/v1/shipments/{sid}", "tracking_number": "620"}
            for sid in shipment_ids
        ],
    }


def _client(*orders) -> MagicMock:
    client = MagicMock()
    client.list_dispatch_orders.return_value = list(orders)
    return client


def _assert_read_only(client: MagicMock) -> None:
    client.create_dispatch_order.assert_not_called()
    client.cancel_dispatch_order.assert_not_called()


class TestSyncInpostPickupsOnce:
    def test_external_dispatch_order_marks_the_draft(self):
        store = MagicMock()
        store.list_drafts.return_value = [_created_draft()]
        # ShipX returns numeric ids; the draft stores strings.
        client = _client(_dispatch(9001, 777), _dispatch(42, "ship-1"))

        stats = sync_inpost_pickups_once(shipping_store=store, client=client)

        assert stats == {"scanned": 1, "marked": 1, "unmatched": 0, "errors": 0}
        store.update_draft.assert_called_once_with(
            "d1", {"pickup_ordered": True, "dispatch_order_id": "42"}
        )
        _assert_read_only(client)

    def test_matches_on_any_parcel_of_a_multi_parcel_draft(self):
        draft = _created_draft(shipment_id="ship-1")
        draft["courier_shipments"].append(
            {"id": "ship-2", "tracking_number": "620B", "package_type": "1-pak"}
        )
        store = MagicMock()
        store.list_drafts.return_value = [draft]
        client = _client(_dispatch("disp-7", "ship-2"))

        stats = sync_inpost_pickups_once(shipping_store=store, client=client)

        assert stats["marked"] == 1
        assert store.update_draft.call_args.args[1]["dispatch_order_id"] == "disp-7"

    def test_matches_on_courier_draft_id_without_shipments(self):
        draft = _created_draft(shipment_id="ship-9")
        draft["courier_shipments"] = []
        store = MagicMock()
        store.list_drafts.return_value = [draft]

        stats = sync_inpost_pickups_once(
            shipping_store=store, client=_client(_dispatch("disp-9", 0, "ship-9"))
        )

        assert stats["marked"] == 1

    def test_cancelled_dispatch_order_does_not_mark(self):
        store = MagicMock()
        store.list_drafts.return_value = [_created_draft()]
        client = _client(
            _dispatch("disp-c", "ship-1", status="canceled"),
            _dispatch("disp-r", "ship-1", status="rejected"),
        )

        stats = sync_inpost_pickups_once(shipping_store=store, client=client)

        assert stats == {"scanned": 1, "marked": 0, "unmatched": 1, "errors": 0}
        store.update_draft.assert_not_called()
        _assert_read_only(client)

    def test_live_order_wins_over_a_cancelled_one_for_the_same_parcel(self):
        store = MagicMock()
        store.list_drafts.return_value = [_created_draft()]
        client = _client(
            _dispatch("old", "ship-1", status="canceled"),
            _dispatch("new", "ship-1", status="new"),
        )

        sync_inpost_pickups_once(shipping_store=store, client=client)

        assert store.update_draft.call_args.args[1]["dispatch_order_id"] == "new"

    def test_drafts_with_pickup_already_ordered_are_skipped(self):
        store = MagicMock()
        store.list_drafts.return_value = [
            _created_draft(pickup_ordered=True, dispatch_order_id="portal-1"),
        ]
        client = _client(_dispatch("other", "ship-1"))

        stats = sync_inpost_pickups_once(shipping_store=store, client=client)

        assert stats == {"scanned": 0, "marked": 0, "unmatched": 0, "errors": 0}
        client.list_dispatch_orders.assert_not_called()
        store.update_draft.assert_not_called()

    def test_only_created_inpost_drafts_are_considered(self):
        store = MagicMock()
        store.list_drafts.return_value = [
            _created_draft("pending", status="pending_confirmation"),
            _created_draft("apaczka", courier="apaczka"),
            _created_draft("cancelled", status="cancelled"),
        ]
        client = _client(_dispatch("x", "ship-1"))

        stats = sync_inpost_pickups_once(shipping_store=store, client=client)

        assert stats["scanned"] == 0
        client.list_dispatch_orders.assert_not_called()

    def test_inpost_api_error_leaves_drafts_unchanged_and_is_logged(self, caplog):
        store = MagicMock()
        store.list_drafts.return_value = [_created_draft()]
        client = MagicMock()
        client.list_dispatch_orders.side_effect = InPostTransientError(
            "InPost server error 503", courier="inpost", action="list_dispatch_orders"
        )

        with caplog.at_level(logging.ERROR, logger="zdrovena.api.routers.inpost_pickup_poller"):
            stats = sync_inpost_pickups_once(shipping_store=store, client=client)

        assert stats == {"scanned": 1, "marked": 0, "unmatched": 0, "errors": 1}
        store.update_draft.assert_not_called()
        assert any("dispatch order lookup failed" in r.getMessage() for r in caplog.records)
        _assert_read_only(client)

    def test_store_write_failure_is_counted_not_raised(self):
        store = MagicMock()
        store.list_drafts.return_value = [_created_draft("a"), _created_draft("b", "ship-2")]
        store.update_draft.side_effect = [RuntimeError("cosmos down"), True]
        client = _client(_dispatch("d", "ship-1", "ship-2"))

        stats = sync_inpost_pickups_once(shipping_store=store, client=client)

        assert stats == {"scanned": 2, "marked": 1, "unmatched": 0, "errors": 1}

    def test_mock_courier_skips_inpost_entirely(self):
        store = MagicMock()
        client = MagicMock()
        with patch.dict("os.environ", {"MOCK_COURIER": "1"}):
            stats = sync_inpost_pickups_once(shipping_store=store, client=client)

        assert stats["scanned"] == 0
        store.list_drafts.assert_not_called()
        client.list_dispatch_orders.assert_not_called()


class TestSyncInpostPickupForDraft:
    def test_marks_one_draft(self):
        store = MagicMock()
        patch_written = sync_inpost_pickup_for_draft(
            _created_draft(), shipping_store=store, client=_client(_dispatch(5, "ship-1"))
        )

        assert patch_written == {"pickup_ordered": True, "dispatch_order_id": "5"}
        store.update_draft.assert_called_once_with("d1", patch_written)

    def test_api_error_returns_none_and_logs(self, caplog):
        store = MagicMock()
        client = MagicMock()
        client.list_dispatch_orders.side_effect = InPostTransientError(
            "boom", courier="inpost", action="list_dispatch_orders"
        )

        with caplog.at_level(logging.ERROR, logger="zdrovena.api.routers.inpost_pickup_poller"):
            result = sync_inpost_pickup_for_draft(
                _created_draft(), shipping_store=store, client=client
            )

        assert result is None
        store.update_draft.assert_not_called()
        assert any("pickup sync failed for draft d1" in r.getMessage() for r in caplog.records)


class TestListDispatchOrders:
    def _response(self, payload):
        resp = MagicMock()
        resp.ok = True
        resp.json.return_value = payload
        return resp

    def test_walks_pages_until_a_short_page(self):
        client = InPostClient("tok", "org-1")
        pages = [
            self._response({"items": [{"id": 1}, {"id": 2}], "count": 3, "per_page": 2}),
            self._response({"items": [{"id": 3}], "count": 3, "per_page": 2}),
        ]
        with patch.object(client._session, "get", side_effect=pages) as get:
            orders = client.list_dispatch_orders(per_page=2)

        assert [o["id"] for o in orders] == [1, 2, 3]
        assert get.call_count == 2
        url = get.call_args.args[0]
        assert url.endswith("/v1/organizations/org-1/dispatch_orders")
        assert get.call_args.kwargs["params"] == {"page": 2, "per_page": 2}

    def test_stops_at_max_pages(self):
        client = InPostClient("tok", "org-1")
        full = self._response({"items": [{"id": 1}], "count": 999})
        with patch.object(client._session, "get", return_value=full) as get:
            client.list_dispatch_orders(per_page=1, max_pages=3)

        assert get.call_count == 3
