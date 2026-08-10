"""Tests for zdrovena.api.routers.inpost_poller.resolve_pending_inpost_once.

ShipX issues a waybill after the create POST returns, so InPost drafts park at
pending_confirmation. Before this worker existed the only thing that resolved
them was a 5s poll in the browser, so closing the tab left drafts without a
tracking number indefinitely.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from zdrovena.api.routers.inpost_poller import resolve_pending_inpost_once


def _pending_draft(draft_id: str = "d1", shipment_id: str = "ship-1") -> dict:
    return {
        "id": draft_id,
        "shopify_order_number": "1700",
        "courier": "inpost",
        "service": "inpost_courier_standard",
        "status": "pending_confirmation",
        "courier_draft_id": shipment_id,
        "courier_shipments": [
            {
                "id": shipment_id,
                "tracking_number": "",
                "package_type": "1-pak",
                "package_number": "1",
            }
        ],
    }


class TestResolvePendingInpostOnce:
    def test_promotes_a_draft_once_shipx_has_the_waybill(self):
        store = MagicMock()
        store.list_drafts.return_value = [_pending_draft()]
        client = MagicMock()
        client.get_shipment.return_value = {"id": "ship-1", "tracking_number": "620DONE"}

        stats = resolve_pending_inpost_once(shipping_store=store, client=client)

        assert stats == {"scanned": 1, "resolved": 1, "still_pending": 0, "errors": 0}
        patch_written = store.update_draft.call_args.args[1]
        assert patch_written["status"] == "created"
        assert patch_written["tracking_number"] == "620DONE"
        assert patch_written["shipment_origin"] == "system"

    def test_resolves_missing_second_parcel_without_losing_pickup_state(self):
        draft = {
            **_pending_draft(),
            "dispatch_order_id": "dispatch-123",
            "pickup_ordered": True,
            "courier_shipments": [
                {
                    "id": "ship-1",
                    "tracking_number": "TRACK-1",
                    "package_type": "1-pak",
                    "package_number": "1",
                },
                {
                    "id": "ship-2",
                    "tracking_number": "",
                    "package_type": "1-pak",
                    "package_number": "2",
                },
            ],
        }
        store = MagicMock()
        store.list_drafts.return_value = [draft]
        client = MagicMock()

        def get_shipment(shipment_id: str) -> dict[str, str]:
            assert shipment_id == "ship-2"
            return {"id": "ship-2", "tracking_number": "TRACK-2"}

        client.get_shipment.side_effect = get_shipment

        stats = resolve_pending_inpost_once(shipping_store=store, client=client)

        assert stats == {"scanned": 1, "resolved": 1, "still_pending": 0, "errors": 0}
        patch_written = store.update_draft.call_args.args[1]
        assert [item["tracking_number"] for item in patch_written["courier_shipments"]] == [
            "TRACK-1",
            "TRACK-2",
        ]
        assert patch_written["dispatch_order_id"] == "dispatch-123"
        assert patch_written["pickup_ordered"] is True
        assert patch_written["status"] == "created"
        client.create_kurier_shipment.assert_not_called()
        client.create_paczkomat_shipment.assert_not_called()

    def test_multi_parcel_draft_stays_pending_when_second_waybill_is_still_missing(self):
        draft = {
            **_pending_draft(),
            "dispatch_order_id": "dispatch-123",
            "pickup_ordered": True,
            "courier_shipments": [
                {
                    "id": "ship-1",
                    "tracking_number": "TRACK-1",
                    "package_type": "1-pak",
                    "package_number": "1",
                },
                {
                    "id": "ship-2",
                    "tracking_number": "",
                    "package_type": "1-pak",
                    "package_number": "2",
                },
            ],
        }
        original_shipments = [dict(shipment) for shipment in draft["courier_shipments"]]
        store = MagicMock()
        store.list_drafts.return_value = [draft]
        client = MagicMock()
        client.get_shipment.return_value = {"id": "ship-2", "tracking_number": None}
        client.wait_for_shipment_confirmation.return_value = {
            "id": "ship-2",
            "tracking_number": None,
        }

        stats = resolve_pending_inpost_once(shipping_store=store, client=client)

        assert stats == {"scanned": 1, "resolved": 0, "still_pending": 1, "errors": 0}
        client.get_shipment.assert_called_once_with("ship-2")
        client.wait_for_shipment_confirmation.assert_called_once_with(
            "ship-2",
            max_attempts=3,
            interval_s=1.0,
        )
        store.update_draft.assert_not_called()
        assert draft["status"] == "pending_confirmation"
        assert draft["courier_shipments"] == original_shipments
        assert draft["dispatch_order_id"] == "dispatch-123"
        assert draft["pickup_ordered"] is True
        client.create_kurier_shipment.assert_not_called()
        client.create_paczkomat_shipment.assert_not_called()

    def test_leaves_the_draft_pending_while_shipx_has_no_waybill(self):
        """Still waiting is the normal case, not an error — and the draft must
        not be promoted to created without a number behind it."""
        store = MagicMock()
        store.list_drafts.return_value = [_pending_draft()]
        client = MagicMock()
        client.get_shipment.return_value = {"id": "ship-1", "tracking_number": None}
        client.wait_for_shipment_confirmation.return_value = {
            "id": "ship-1",
            "tracking_number": None,
        }

        stats = resolve_pending_inpost_once(shipping_store=store, client=client)

        assert stats["still_pending"] == 1
        assert stats["resolved"] == 0
        store.update_draft.assert_not_called()

    def test_never_creates_a_second_shipment(self):
        """The whole point of resuming: a draft that already has a ShipX id must
        never be POSTed again."""
        store = MagicMock()
        store.list_drafts.return_value = [_pending_draft()]
        client = MagicMock()
        client.get_shipment.return_value = {"id": "ship-1", "tracking_number": "620DONE"}

        resolve_pending_inpost_once(shipping_store=store, client=client)

        client.create_kurier_shipment.assert_not_called()
        client.create_paczkomat_shipment.assert_not_called()

    def test_ignores_drafts_that_are_not_pending_inpost(self):
        store = MagicMock()
        store.list_drafts.return_value = [
            {**_pending_draft("d-created"), "status": "created"},
            {**_pending_draft("d-allegro"), "courier": "allegro_delivery"},
            {**_pending_draft("d-no-id"), "courier_draft_id": ""},
        ]
        client = MagicMock()

        stats = resolve_pending_inpost_once(shipping_store=store, client=client)

        assert stats["scanned"] == 0
        client.get_shipment.assert_not_called()

    def test_one_bad_shipment_does_not_stop_the_rest(self):
        store = MagicMock()
        store.list_drafts.return_value = [
            _pending_draft("d1", "ship-bad"),
            _pending_draft("d2", "ship-good"),
        ]
        client = MagicMock()
        client.get_shipment.side_effect = [
            RuntimeError("ShipX exploded"),
            {"id": "ship-good", "tracking_number": "620OK"},
        ]

        stats = resolve_pending_inpost_once(shipping_store=store, client=client)

        assert stats["errors"] == 1
        assert stats["resolved"] == 1

    def test_store_read_failure_is_counted_not_raised(self):
        store = MagicMock()
        store.list_drafts.side_effect = RuntimeError("table unreachable")

        stats = resolve_pending_inpost_once(shipping_store=store, client=MagicMock())

        assert stats["errors"] == 1
        assert stats["scanned"] == 0

    def test_asks_for_more_than_the_default_page_of_drafts(self):
        """A draft stuck pending is exactly the kind old enough to fall outside
        list_drafts' default 200-row window."""
        store = MagicMock()
        store.list_drafts.return_value = []

        resolve_pending_inpost_once(shipping_store=store, client=MagicMock())

        assert store.list_drafts.call_args.kwargs["limit"] > 200

    def test_mock_courier_skips_the_whole_cycle(self, monkeypatch):
        monkeypatch.setenv("MOCK_COURIER", "1")
        store = MagicMock()
        client = MagicMock()

        stats = resolve_pending_inpost_once(shipping_store=store, client=client)

        assert stats["scanned"] == 0
        store.list_drafts.assert_not_called()

    def test_persist_failure_does_not_claim_the_draft_was_resolved(self):
        store = MagicMock()
        store.list_drafts.return_value = [_pending_draft()]
        store.update_draft.side_effect = RuntimeError("write failed")
        client = MagicMock()
        client.get_shipment.return_value = {"id": "ship-1", "tracking_number": "620DONE"}

        with patch("zdrovena.api.routers.inpost_poller._emit_tracking_assigned") as emit:
            stats = resolve_pending_inpost_once(shipping_store=store, client=client)

        assert stats["resolved"] == 0
        assert stats["errors"] == 1
        emit.assert_not_called()
