"""Integration tests: execute_draft with courier='allegro_delivery' (Ship with Allegro).

For source='allegro' drafts we call _run_allegro_delivery instead of InPost/Apaczka.
Flow: get_delivery_proposal → create_ship_with_allegro_shipment → poll → get_shipment
     → extract waybill → get_label → optional pickup ordering.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from zdrovena.api.routers.webhooks import (
    _allegro_carrier_id_for_courier,
    _maybe_push_tracking_to_allegro,
    _run_allegro_delivery,
)
from zdrovena.common.shipping_exceptions import CourierServerError

_PROPOSAL = {
    "suggestedInput": {
        "sender": {
            "name": "Nadawca",
            "street": "Główna 30",
            "postalCode": "10-200",
            "city": "Warszawa",
            "countryCode": "PL",
            "email": "sender@mail.com",
            "phone": "500600700",
        },
        "receiver": {
            "name": "Jan Kowalski",
            "street": "Testowa 1",
            "postalCode": "00-001",
            "city": "Warszawa",
            "countryCode": "PL",
            "email": "j@k.pl",
            "phone": "600000000",
        },
        "cashOnDelivery": {"amount": "125.00", "currency": "PLN"},
    },
}


# ── _run_allegro_delivery ─────────────────────────────────────────────────────


class TestRunAllegroDelivery:
    def _draft(self, **overrides):
        base = {
            "id": "d1",
            "source": "allegro",
            "external_order_id": "ORD-1",
            "shopify_order_number": "ALG-1",
            "receiver": {
                "first_name": "Jan",
                "last_name": "Kowalski",
                "email": "j@k.pl",
                "phone": "600000000",
                "locker_id": "WAW01A",
            },
            "shipping_address": {
                "street": "Testowa 1",
                "building_number": "1",
                "city": "Warszawa",
                "post_code": "00-001",
            },
            "packages_breakdown": [{"type": "1-pak", "qty": 1}],
            "allegro_delivery_method_id": "svc-inpost-locker",
            "allegro_credentials_id": None,
            "allegro_sending_method": "parcel_locker",
        }
        base.update(overrides)
        return base

    def test_full_success_flow(self):
        """Happy path: create → poll SUCCESS → get shipment → extract waybill → label."""
        client = MagicMock()
        client.get_delivery_proposal.return_value = _PROPOSAL
        client.create_ship_with_allegro_shipment.return_value = {
            "commandId": "cmd-1",
            "status": "IN_PROGRESS",
        }
        client.wait_for_ship_with_allegro_shipment.return_value = "ship-42"
        client.get_ship_with_allegro_shipment.return_value = {
            "id": "ship-42",
            "packages": [
                {"transportingInfo": [{"carrierId": "INPOST", "carrierWaybill": "620XYZ"}]}
            ],
        }
        client.extract_shipment_waybill = MagicMock(return_value=("INPOST", "620XYZ"))

        with patch(
            "zdrovena.api.routers.webhooks._get_allegro_client",
            return_value=client,
        ):
            result = _run_allegro_delivery(self._draft(), MagicMock())

        client.create_ship_with_allegro_shipment.assert_called_once()
        client.wait_for_ship_with_allegro_shipment.assert_called_once()
        # command_id is generated inside; ensure it's passed as the first (only) positional arg
        called_cmd = client.wait_for_ship_with_allegro_shipment.call_args[0][0]
        assert called_cmd == client.create_ship_with_allegro_shipment.call_args.kwargs["command_id"]
        client.get_ship_with_allegro_shipment.assert_called_once_with("ship-42")

        assert result["status"] == "created"
        assert result["courier_draft_id"] == "ship-42"
        assert result["tracking_number"] == "620XYZ"
        assert result["error"] is None

    def test_passes_delivery_method_and_sending_method(self):
        client = MagicMock()
        client.get_delivery_proposal.return_value = _PROPOSAL
        client.create_ship_with_allegro_shipment.return_value = {"commandId": "cmd-1"}
        client.wait_for_ship_with_allegro_shipment.return_value = "ship-1"
        client.get_ship_with_allegro_shipment.return_value = {
            "packages": [{"transportingInfo": [{"carrierId": "INPOST", "carrierWaybill": "W1"}]}]
        }
        client.extract_shipment_waybill = MagicMock(return_value=("INPOST", "W1"))

        with patch(
            "zdrovena.api.routers.webhooks._get_allegro_client",
            return_value=client,
        ):
            _run_allegro_delivery(
                self._draft(
                    allegro_delivery_method_id="svc-dpd",
                    allegro_credentials_id="own-agreement-42",
                    allegro_sending_method=None,
                ),
                MagicMock(),
            )

        call = client.create_ship_with_allegro_shipment.call_args
        assert call.kwargs["delivery_method_id"] == "svc-dpd"
        assert call.kwargs["credentials_id"] == "own-agreement-42"
        assert call.kwargs["order_id"] == "ORD-1"
        # sender/receiver blocks come from the delivery proposal.
        suggested = _PROPOSAL["suggestedInput"]
        assert call.kwargs["sender"] == suggested["sender"]
        assert call.kwargs["receiver"]["name"] == suggested["receiver"]["name"]
        assert call.kwargs["suggested_input"] == suggested
        # sending_method is now mapped to additionalProperties; when None it's omitted.
        assert call.kwargs.get("additional_properties") is None

    def test_maps_at_point_sending_to_additional_service(self):
        """At-point sending is represented by the current sendingAtPoint service."""
        client = MagicMock()
        client.get_delivery_proposal.return_value = _PROPOSAL
        client.create_ship_with_allegro_shipment.return_value = {"commandId": "cmd-1"}
        client.wait_for_ship_with_allegro_shipment.return_value = "ship-1"
        client.get_ship_with_allegro_shipment.return_value = {
            "packages": [{"transportingInfo": [{"carrierId": "INPOST", "carrierWaybill": "W1"}]}]
        }
        client.extract_shipment_waybill = MagicMock(return_value=("INPOST", "W1"))

        with patch(
            "zdrovena.api.routers.webhooks._get_allegro_client",
            return_value=client,
        ):
            # default _draft() already sets allegro_sending_method='parcel_locker'
            _run_allegro_delivery(self._draft(), MagicMock())

        call = client.create_ship_with_allegro_shipment.call_args
        assert call.kwargs["additional_services"] == ["sendingAtPoint"]
        assert call.kwargs["additional_properties"] is None

    def test_ignores_unknown_sending_method(self):
        """P1-2: unknown allegro_sending_method values are silently dropped."""
        client = MagicMock()
        client.get_delivery_proposal.return_value = _PROPOSAL
        client.create_ship_with_allegro_shipment.return_value = {"commandId": "cmd-1"}
        client.wait_for_ship_with_allegro_shipment.return_value = "ship-1"
        client.get_ship_with_allegro_shipment.return_value = {
            "packages": [{"transportingInfo": [{"carrierId": "INPOST", "carrierWaybill": "W1"}]}]
        }
        client.extract_shipment_waybill = MagicMock(return_value=("INPOST", "W1"))

        with patch(
            "zdrovena.api.routers.webhooks._get_allegro_client",
            return_value=client,
        ):
            _run_allegro_delivery(
                self._draft(allegro_sending_method="bogus_value"),
                MagicMock(),
            )

        call = client.create_ship_with_allegro_shipment.call_args
        assert call.kwargs.get("additional_properties") is None

    def test_passes_pickup_point_for_locker(self):
        client = MagicMock()
        client.get_delivery_proposal.return_value = _PROPOSAL
        client.create_ship_with_allegro_shipment.return_value = {"commandId": "cmd-1"}
        client.wait_for_ship_with_allegro_shipment.return_value = "ship-1"
        client.get_ship_with_allegro_shipment.return_value = {
            "packages": [{"transportingInfo": [{"carrierId": "INPOST", "carrierWaybill": "W1"}]}]
        }
        client.extract_shipment_waybill = MagicMock(return_value=("INPOST", "W1"))

        with patch(
            "zdrovena.api.routers.webhooks._get_allegro_client",
            return_value=client,
        ):
            _run_allegro_delivery(self._draft(), MagicMock())

        call = client.create_ship_with_allegro_shipment.call_args
        # Locker code is now carried inside the receiver block as `point`.
        assert call.kwargs["receiver"]["point"] == "WAW01A"
        assert "pickup_point_id" not in call.kwargs

    def test_passes_each_physical_package(self):
        client = MagicMock()
        client.get_delivery_proposal.return_value = _PROPOSAL
        client.create_ship_with_allegro_shipment.return_value = {"commandId": "cmd-1"}
        client.wait_for_ship_with_allegro_shipment.return_value = "ship-1"
        client.get_ship_with_allegro_shipment.return_value = {
            "packages": [{"transportingInfo": [{"carrierId": "INPOST", "carrierWaybill": "W1"}]}]
        }
        client.extract_shipment_waybill.return_value = ("INPOST", "W1")

        with patch(
            "zdrovena.api.routers.webhooks._get_allegro_client",
            return_value=client,
        ):
            _run_allegro_delivery(
                self._draft(
                    packages_count=2,
                    packages_breakdown=[{"type": "1-pak", "qty": 2}],
                ),
                MagicMock(),
            )

        packages = client.create_ship_with_allegro_shipment.call_args.kwargs["packages"]
        assert len(packages) == 2
        assert all(package["weight"] == {"value": 6.0, "unit": "KILOGRAMS"} for package in packages)

    def test_creates_pickup_new_format(self):
        """pickup_date + new-format pickupTimes -> passes pickup_time to client."""
        client = MagicMock()
        client.get_delivery_proposal.return_value = _PROPOSAL
        client.create_ship_with_allegro_shipment.return_value = {"commandId": "cmd-1"}
        client.wait_for_ship_with_allegro_shipment.return_value = "ship-42"
        client.get_ship_with_allegro_shipment.return_value = {
            "packages": [{"transportingInfo": [{"carrierId": "INPOST", "carrierWaybill": "W1"}]}]
        }
        client.extract_shipment_waybill = MagicMock(return_value=("INPOST", "W1"))
        client.get_ship_with_allegro_pickup_proposals.return_value = [
            {
                "date": "2026-07-05",
                "minTime": "08:00",
                "maxTime": "12:00",
                "shipmentId": "ship-42",
            }
        ]

        with patch(
            "zdrovena.api.routers.webhooks._get_allegro_client",
            return_value=client,
        ):
            result = _run_allegro_delivery(
                self._draft(),
                MagicMock(),
                pickup_date="2026-07-05",
            )

        sender = _PROPOSAL["suggestedInput"]["sender"]
        client.get_ship_with_allegro_pickup_proposals.assert_called_once_with(
            ["ship-42"],
            ready_date="2026-07-05",
            address=sender,
        )
        client.create_ship_with_allegro_pickup.assert_called_once()
        pickup_call = client.create_ship_with_allegro_pickup.call_args
        assert pickup_call.kwargs["pickup_time"] == {
            "date": "2026-07-05",
            "minTime": "08:00",
            "maxTime": "12:00",
        }
        assert pickup_call.kwargs["shipment_ids"] == ["ship-42"]
        assert pickup_call.kwargs["address"] == sender
        # Legacy field must not be passed.
        assert "proposal_item_id" not in pickup_call.kwargs
        assert result["pickup_ordered"] is True

    def test_creates_pickup_legacy_fallback(self):
        """Sandbox / older-server response with only proposal_item id — legacy path."""
        client = MagicMock()
        client.get_delivery_proposal.return_value = _PROPOSAL
        client.create_ship_with_allegro_shipment.return_value = {"commandId": "cmd-1"}
        client.wait_for_ship_with_allegro_shipment.return_value = "ship-42"
        client.get_ship_with_allegro_shipment.return_value = {
            "packages": [{"transportingInfo": [{"carrierId": "INPOST", "carrierWaybill": "W1"}]}]
        }
        client.extract_shipment_waybill = MagicMock(return_value=("INPOST", "W1"))
        client.get_ship_with_allegro_pickup_proposals.return_value = [
            {"id": "prop-1", "shipmentId": "ship-42"}
        ]

        with patch(
            "zdrovena.api.routers.webhooks._get_allegro_client",
            return_value=client,
        ):
            result = _run_allegro_delivery(
                self._draft(),
                MagicMock(),
                pickup_date="2026-07-02",
            )

        pickup_call = client.create_ship_with_allegro_pickup.call_args
        assert pickup_call.kwargs["proposal_item_id"] == "prop-1"
        assert pickup_call.kwargs["address"] == _PROPOSAL["suggestedInput"]["sender"]
        assert "pickup_time" not in pickup_call.kwargs
        assert result["pickup_ordered"] is True

    def test_no_pickup_when_pickup_date_absent(self):
        client = MagicMock()
        client.get_delivery_proposal.return_value = _PROPOSAL
        client.create_ship_with_allegro_shipment.return_value = {"commandId": "cmd-1"}
        client.wait_for_ship_with_allegro_shipment.return_value = "ship-42"
        client.get_ship_with_allegro_shipment.return_value = {
            "packages": [{"transportingInfo": [{"carrierId": "X", "carrierWaybill": "W"}]}]
        }
        client.extract_shipment_waybill = MagicMock(return_value=("X", "W"))

        with patch(
            "zdrovena.api.routers.webhooks._get_allegro_client",
            return_value=client,
        ):
            result = _run_allegro_delivery(self._draft(), MagicMock())

        client.get_ship_with_allegro_pickup_proposals.assert_not_called()
        client.create_ship_with_allegro_pickup.assert_not_called()
        assert result["pickup_ordered"] is False

    def test_pickup_failure_does_not_abort_shipment(self):
        """If pickup fails after shipment is created, we still return the shipment info."""
        client = MagicMock()
        client.get_delivery_proposal.return_value = _PROPOSAL
        client.create_ship_with_allegro_shipment.return_value = {"commandId": "cmd-1"}
        client.wait_for_ship_with_allegro_shipment.return_value = "ship-42"
        client.get_ship_with_allegro_shipment.return_value = {
            "packages": [{"transportingInfo": [{"carrierId": "X", "carrierWaybill": "W"}]}]
        }
        client.extract_shipment_waybill = MagicMock(return_value=("X", "W"))
        client.get_ship_with_allegro_pickup_proposals.side_effect = CourierServerError(
            courier="allegro", status=503
        )

        with patch(
            "zdrovena.api.routers.webhooks._get_allegro_client",
            return_value=client,
        ):
            result = _run_allegro_delivery(
                self._draft(),
                MagicMock(),
                pickup_date="2026-07-02",
            )

        assert result["status"] == "created"
        assert result["tracking_number"] == "W"
        assert result["pickup_ordered"] is False


# ── Update: allegro-sourced drafts should NOT push tracking back via /order/../shipments ──


class TestNoPushForAllegroDelivery:
    """Ship with Allegro tracks the waybill server-side. No manual push needed."""

    def test_allegro_delivery_courier_skips_push(self):
        """When courier='allegro_delivery', do NOT call create_shipment (push)."""
        draft = {
            "id": "d",
            "source": "allegro",
            "external_order_id": "ORD-1",
            "courier": "allegro_delivery",
            "tracking_number": "620XYZ",
        }
        client = MagicMock()
        with patch(
            "zdrovena.api.routers.webhooks._get_allegro_client",
            return_value=client,
        ):
            _maybe_push_tracking_to_allegro(draft)
        # Ship with Allegro auto-syncs waybill; no need to POST /shipments
        client.create_shipment.assert_not_called()


class TestCarrierIdMapping:
    def test_allegro_delivery_courier_maps_to_other(self):
        """If ever pushed (fallback), allegro_delivery maps to OTHER — but push is skipped."""
        # Behaviour: for backward-compat we accept the mapping; the guard above prevents actual push.
        assert _allegro_carrier_id_for_courier("allegro_delivery") == "OTHER"


class TestPendingConfirmation:
    """Non-blocking flow: gdy create-command jeszcze IN_PROGRESS po krótkim polling —
    zwracamy status='pending_confirmation' zamiast blokować request."""

    def _draft(self, **overrides):
        base = {
            "id": "draft-async",
            "external_order_id": "AL-ASYNC",
            "shopify_order_number": "9101",
            "receiver": {"locker_id": "WAW10A"},
            "packages_breakdown": [{"type": "1-pak", "qty": 1}],
            "allegro_delivery_method_id": "svc-inpost-locker",
            "allegro_credentials_id": None,
            "allegro_sending_method": "parcel_locker",
        }
        base.update(overrides)
        return base

    def test_timeout_returns_pending_confirmation(self):
        """AllegroCommandPending (osobny podtyp) — zwracamy pending_confirmation bez sprawdzania stringu."""
        from zdrovena.common.shipping_exceptions import AllegroCommandPending

        client = MagicMock()
        client.get_delivery_proposal.return_value = _PROPOSAL
        client.create_ship_with_allegro_shipment.return_value = {"commandId": "cmd-async-1"}
        client.wait_for_ship_with_allegro_shipment.side_effect = AllegroCommandPending(
            command_id="cmd-async-1",
        )

        with patch(
            "zdrovena.api.routers.webhooks._get_allegro_client",
            return_value=client,
        ):
            result = _run_allegro_delivery(self._draft(), MagicMock())

        assert result["status"] == "pending_confirmation"
        assert result["courier_draft_id"] is None
        assert result["tracking_number"] is None
        assert result["pickup_ordered"] is False
        assert result["error"] is None
        assert result["allegro_command_id"] == "cmd-async-1"
        # Nie wołamy get_ship_with_allegro_shipment przy pending
        client.get_ship_with_allegro_shipment.assert_not_called()

    def test_pending_draft_does_not_create_second_command(self):
        """Duplicate guard: draft już z allegro_command_id + status pending_confirmation —
        NIE wolno tworzyć drugiej komendy Allegro (regression: podwójna wysyłka)."""
        client = MagicMock()

        with patch(
            "zdrovena.api.routers.webhooks._get_allegro_client",
            return_value=client,
        ):
            draft = self._draft(
                allegro_command_id="cmd-existing-42",
                status="pending_confirmation",
            )
            result = _run_allegro_delivery(draft, MagicMock())

        assert result["status"] == "pending_confirmation"
        assert result["allegro_command_id"] == "cmd-existing-42"
        assert result["courier_draft_id"] is None
        assert result["tracking_number"] is None
        # KLUCZOWE: nie wołamy create ani wait
        client.create_ship_with_allegro_shipment.assert_not_called()
        client.wait_for_ship_with_allegro_shipment.assert_not_called()
        client.get_ship_with_allegro_shipment.assert_not_called()

    def test_hard_error_bubbles_up(self):
        """Twarde ERROR (nie timeout) — wyjątek leci dalej."""
        from zdrovena.common.shipping_exceptions import AllegroBusinessError

        client = MagicMock()
        client.get_delivery_proposal.return_value = _PROPOSAL
        client.create_ship_with_allegro_shipment.return_value = {"commandId": "cmd-err"}
        client.wait_for_ship_with_allegro_shipment.side_effect = AllegroBusinessError(
            detail="create-command ERROR: invalid method",
            action="wait_for_ship_with_allegro_shipment",
        )

        with (
            patch(
                "zdrovena.api.routers.webhooks._get_allegro_client",
                return_value=client,
            ),
            pytest.raises(AllegroBusinessError),
        ):
            _run_allegro_delivery(self._draft(), MagicMock())
