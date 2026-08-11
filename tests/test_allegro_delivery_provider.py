"""Direct characterization tests for Allegro Delivery planning."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from zdrovena.common.shipping_exceptions import AllegroBusinessError, CourierTransientError
from zdrovena.shipping.providers.allegro_delivery import (
    ALLEGRO_INPOST_SENDING_METHODS,
    allegro_call_spec,
    allegro_payload_plan,
)

_DRAFT: dict[str, Any] = {
    "id": "allegro-provider-test",
    "shopify_order_number": "1053",
    "external_order_id": "allegro-order-9",
    "courier": "allegro_delivery",
    "service": "allegro_delivery",
    "receiver": {
        "first_name": "Ola",
        "last_name": "Wisniewska",
        "locker_id": "LOD01A",
    },
    "shipping_address": {
        "street": "Lipowa",
        "building_number": "3",
        "city": "Lodz",
    },
    "packages_breakdown": [
        {"type": "2-pak", "qty": 2},
        {"type": "pół-pak", "qty": 1},
    ],
    "allegro_delivery_method_id": "method-inpost-locker",
    "allegro_credentials_id": None,
    "allegro_sending_method": "parcel_locker",
}

_PROPOSAL: dict[str, Any] = {
    "suggestedInput": {
        "sender": {
            "name": "Maria Gryzło ZDROVENA",
            "street": "Cieszynska 6/12",
            "postalCode": "30-015",
            "city": "Kraków",
            "countryCode": "PL",
        },
        "receiver": {
            "name": "Ola Wisniewska",
            "street": "Lipowa 3",
            "postalCode": "90-001",
            "city": "Lodz",
            "countryCode": "PL",
        },
    }
}


class _ReadOnlyProposalClient:
    def __init__(self, proposal: dict[str, Any]) -> None:
        self.proposal = proposal
        self.proposal_order_ids: list[str] = []
        self.provider_writes: list[str] = []

    def get_delivery_proposal(self, order_id: str) -> dict[str, Any]:
        self.proposal_order_ids.append(order_id)
        return self.proposal

    def create_ship_with_allegro_shipment(self, **kwargs: Any) -> None:
        self.provider_writes.append("shipment")

    def create_ship_with_allegro_pickup(self, **kwargs: Any) -> None:
        self.provider_writes.append("pickup")


def _draft(**overrides: Any) -> dict[str, Any]:
    draft = dict(_DRAFT)
    draft["receiver"] = dict(_DRAFT["receiver"])
    draft["packages_breakdown"] = [dict(item) for item in _DRAFT["packages_breakdown"]]
    draft.update(overrides)
    return draft


def test_call_spec_matches_exact_legacy_shape() -> None:
    proposal = {
        "suggestedInput": {
            "sender": dict(_PROPOSAL["suggestedInput"]["sender"]),
            "receiver": dict(_PROPOSAL["suggestedInput"]["receiver"]),
        }
    }

    result = allegro_call_spec(_draft(), proposal)

    assert result == {
        "order_id": "allegro-order-9",
        "delivery_method_id": "method-inpost-locker",
        "credentials_id": None,
        "packages": [
            {
                "type": "PACKAGE",
                "length": {"value": 40, "unit": "CENTIMETER"},
                "width": {"value": 30, "unit": "CENTIMETER"},
                "height": {"value": 20, "unit": "CENTIMETER"},
                "weight": {"value": 27.0, "unit": "KILOGRAMS"},
            }
        ],
        "sender": {
            "name": "Maria Gryzło ZDROVENA",
            "street": "Cieszynska 6/12",
            "postalCode": "30-015",
            "city": "Kraków",
            "countryCode": "PL",
        },
        "receiver": {
            "name": "Ola Wisniewska",
            "street": "Lipowa 3",
            "postalCode": "90-001",
            "city": "Lodz",
            "countryCode": "PL",
            "point": "LOD01A",
        },
        "additional_properties": {"inpost#sendingMethod": "parcel_locker"},
    }
    assert result["sender"] is proposal["suggestedInput"]["sender"]
    assert "point" not in proposal["suggestedInput"]["receiver"]


def test_reference_number_source_is_external_order_id_not_local_or_display_id() -> None:
    draft = _draft(
        id="local-draft-uuid",
        external_order_id="allegro-checkout-uuid",
        shopify_order_number="operator-display-value",
    )

    result = allegro_call_spec(draft, _PROPOSAL)

    assert result["order_id"] == "allegro-checkout-uuid"
    assert result["order_id"] != draft["id"]
    assert result["order_id"] != draft["shopify_order_number"]


@pytest.mark.parametrize(
    "sending_method",
    sorted(ALLEGRO_INPOST_SENDING_METHODS),
)
def test_call_spec_preserves_each_inpost_sending_method(sending_method: str) -> None:
    result = allegro_call_spec(
        _draft(allegro_sending_method=sending_method),
        _PROPOSAL,
    )

    assert result["additional_properties"] == {"inpost#sendingMethod": sending_method}


def test_call_spec_omits_point_and_ignores_unknown_sending_method() -> None:
    result = allegro_call_spec(
        _draft(
            receiver={"locker_id": ""},
            allegro_delivery_method_id="",
            allegro_sending_method="unknown",
        ),
        _PROPOSAL,
    )

    assert "point" not in result["receiver"]
    assert result["delivery_method_id"] is None
    assert result["additional_properties"] is None


@pytest.mark.parametrize(
    ("proposal", "message"),
    [
        ({}, "Allegro delivery proposal has no suggestedInput object"),
        (
            {"suggestedInput": {}},
            "Allegro delivery proposal has no suggestedInput.sender or receiver",
        ),
        (
            {"suggestedInput": {"sender": {"name": "Sender"}}},
            "Allegro delivery proposal has no suggestedInput.sender or receiver",
        ),
    ],
)
def test_call_spec_preserves_invalid_proposal_errors(
    proposal: dict[str, Any], message: str
) -> None:
    with pytest.raises(AllegroBusinessError, match=message) as raised:
        allegro_call_spec(_draft(), proposal)

    assert raised.value.action == "get_delivery_proposal"


def test_payload_plan_reads_proposal_once_and_performs_no_writes_or_uuid_generation() -> None:
    client = _ReadOnlyProposalClient(_PROPOSAL)

    with patch("uuid.uuid4", side_effect=AssertionError("preview generated a command UUID")):
        plan = allegro_payload_plan(_draft(), client)

    assert client.proposal_order_ids == ["allegro-order-9"]
    assert client.provider_writes == []
    assert plan == [
        {
            "service": "allegro_delivery",
            "package_type": "allegro",
            "package_number": 1,
            "reference": "allegro-order-9",
            "payload": allegro_call_spec(_draft(), _PROPOSAL),
        }
    ]


def test_payload_plan_validates_external_order_id_before_provider_lookup() -> None:
    client = _ReadOnlyProposalClient(_PROPOSAL)

    with pytest.raises(RuntimeError, match="Ship with Allegro requires external_order_id"):
        allegro_payload_plan(_draft(external_order_id=""), client)

    assert client.proposal_order_ids == []
    assert client.provider_writes == []


def test_payload_plan_propagates_provider_error_unchanged() -> None:
    error = CourierTransientError(
        "Allegro unavailable",
        courier="allegro",
        action="delivery_proposal",
    )
    client = MagicMock()
    client.get_delivery_proposal.side_effect = error

    with pytest.raises(CourierTransientError) as raised:
        allegro_payload_plan(_draft(), client)

    assert raised.value is error


def test_router_preview_composes_get_client_with_provider_planner(monkeypatch) -> None:
    from zdrovena.api.routers import webhooks

    client = _ReadOnlyProposalClient(_PROPOSAL)
    monkeypatch.setattr(webhooks, "_get_allegro_client", lambda: client)

    preview = webhooks._execution_preview(_draft())

    assert client.proposal_order_ids == ["allegro-order-9"]
    assert preview["preview_available"] is True
    assert preview["sender"] == _PROPOSAL["suggestedInput"]["sender"]
    assert preview["parcels"][0]["payload"]["receiver"]["point"] == "LOD01A"


def test_router_preview_preserves_fail_closed_provider_error_handling(monkeypatch) -> None:
    from zdrovena.api.routers import webhooks

    client = MagicMock()
    client.get_delivery_proposal.side_effect = CourierTransientError(
        "Allegro unavailable",
        courier="allegro",
        action="delivery_proposal",
    )
    monkeypatch.setattr(webhooks, "_get_allegro_client", lambda: client)

    preview = webhooks._execution_preview(_draft())

    client.get_delivery_proposal.assert_called_once_with("allegro-order-9")
    assert preview["preview_available"] is False
    assert preview["parcels"] == []
    assert preview["sender"] == {}
    assert "Allegro" in preview["note"]
