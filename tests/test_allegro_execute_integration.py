"""Tests for Allegro-source drafts flowing through execute_draft.

When a draft has ``source == 'allegro'``, executing it must:
1. Create the shipment via the same InPost/Apaczka client used for Shopify
2. After success, push the tracking number back to Allegro via
   ``AllegroClient.create_shipment(order_id, carrier_id, waybill)``.

We wire the Allegro push through a helper ``_maybe_push_tracking_to_allegro``
so it can be tested in isolation without spinning up FastAPI.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from zdrovena.api.routers.webhooks import (
    _allegro_carrier_id_for_courier,
    _maybe_push_tracking_to_allegro,
)


class TestAllegroCarrierId:
    def test_inpost_maps_to_inpost_carrier(self):
        assert _allegro_carrier_id_for_courier("inpost") == "INPOST"

    def test_apaczka_maps_to_other(self):
        # Apaczka is not a native Allegro carrier — must fall back to OTHER
        assert _allegro_carrier_id_for_courier("apaczka") == "OTHER"

    def test_unknown_courier_maps_to_other(self):
        assert _allegro_carrier_id_for_courier("dpd") == "OTHER"


class TestMaybePushTrackingToAllegro:
    def _draft(self, source="allegro", tracking="TRK1", courier="inpost"):
        return {
            "id": "d1",
            "source": source,
            "external_order_id": "af1",
            "courier": courier,
            "tracking_number": tracking,
        }

    def test_shopify_source_skipped(self):
        client = MagicMock()
        with patch(
            "zdrovena.api.routers.webhooks._get_allegro_client",
            return_value=client,
        ):
            _maybe_push_tracking_to_allegro(self._draft(source="shopify"))
        client.create_shipment.assert_not_called()

    def test_allegro_source_pushes_tracking(self):
        client = MagicMock()
        with patch(
            "zdrovena.api.routers.webhooks._get_allegro_client",
            return_value=client,
        ):
            _maybe_push_tracking_to_allegro(self._draft())
        client.create_shipment.assert_called_once_with(
            order_id="af1",
            carrier_id="INPOST",
            waybill="TRK1",
        )

    def test_no_tracking_number_skips_push(self):
        client = MagicMock()
        with patch(
            "zdrovena.api.routers.webhooks._get_allegro_client",
            return_value=client,
        ):
            _maybe_push_tracking_to_allegro(self._draft(tracking=None))
        client.create_shipment.assert_not_called()

    def test_apaczka_pushes_with_other_carrier(self):
        client = MagicMock()
        with patch(
            "zdrovena.api.routers.webhooks._get_allegro_client",
            return_value=client,
        ):
            _maybe_push_tracking_to_allegro(self._draft(courier="apaczka"))
        client.create_shipment.assert_called_once_with(
            order_id="af1",
            carrier_id="OTHER",
            waybill="TRK1",
        )

    def test_push_error_does_not_raise(self):
        client = MagicMock()
        client.create_shipment.side_effect = RuntimeError("boom")
        with patch(
            "zdrovena.api.routers.webhooks._get_allegro_client",
            return_value=client,
        ):
            # Must NOT raise — the draft is already saved, we log and move on
            _maybe_push_tracking_to_allegro(self._draft())

    def test_missing_external_order_id_skips(self):
        client = MagicMock()
        draft = self._draft()
        draft["external_order_id"] = ""
        with patch(
            "zdrovena.api.routers.webhooks._get_allegro_client",
            return_value=client,
        ):
            _maybe_push_tracking_to_allegro(draft)
        client.create_shipment.assert_not_called()

    def test_client_none_when_no_credentials(self):
        # If _get_allegro_client returns None (no secrets configured), no crash
        with patch(
            "zdrovena.api.routers.webhooks._get_allegro_client",
            return_value=None,
        ):
            _maybe_push_tracking_to_allegro(self._draft())  # no exception
