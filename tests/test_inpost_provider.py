"""Direct tests for HTTP-neutral InPost planning and resume behavior."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from zdrovena.common.shipping_exceptions import InPostBusinessError
from zdrovena.common.shipping_parcels import PARCEL_SPECS
from zdrovena.shipping.providers.inpost import (
    inpost_call_specs,
    inpost_payload_plan,
    is_resumable_inpost_draft,
    pending_inpost_call_specs,
    resume_inpost_shipment,
)

_SENDER = {
    "name": "Zdrovena",
    "firstname": "",
    "lastname": "Zdrovena",
    "street": "Testowa",
    "building_number": "1",
    "city": "Kraków",
    "post_code": "30-001",
    "phone": "500000000",
    "email": "sender@example.test",
}


def _draft(**overrides: Any) -> dict[str, Any]:
    draft: dict[str, Any] = {
        "id": "inpost-provider-test",
        "shopify_order_number": "1700",
        "courier": "inpost",
        "service": "inpost_courier_standard",
        "receiver": {
            "first_name": "Jan",
            "last_name": "Kowalski",
            "email": "jan@example.test",
            "phone": "600100200",
            "locker_id": "KRA01A",
        },
        "shipping_address": {
            "street": "Kwiatowa",
            "building_number": "7",
            "flat_number": "2",
            "city": "Warszawa",
            "post_code": "00-001",
        },
        "packages_breakdown": [{"type": "1-pak", "qty": 1}],
        "courier_shipments": [],
    }
    draft.update(overrides)
    return draft


def _shipment_patch(shipments: list[dict[str, str]]) -> dict[str, Any]:
    first = shipments[0] if shipments else {}
    confirmed = bool(shipments) and all(
        str(shipment.get("tracking_number") or "").strip() for shipment in shipments
    )
    return {
        "courier_draft_id": first.get("id"),
        "courier_shipments": shipments,
        "dispatch_order_id": None,
        "tracking_number": first.get("tracking_number"),
        "status": "created" if confirmed else "pending_confirmation",
        "pickup_ordered": False,
        "error": None,
    }


class _RecordingPayloadBuilder:
    def __init__(self) -> None:
        self.kurier_calls: list[dict[str, Any]] = []
        self.paczkomat_calls: list[dict[str, Any]] = []

    def build_kurier_payload(self, **kwargs: Any) -> dict[str, Any]:
        self.kurier_calls.append(kwargs)
        return {"kind": "kurier", "reference": kwargs["reference"]}

    def build_paczkomat_payload(self, **kwargs: Any) -> dict[str, Any]:
        self.paczkomat_calls.append(kwargs)
        return {"kind": "paczkomat", "reference": kwargs["reference"]}


def test_call_specs_preserve_input_order_and_per_type_numbering() -> None:
    draft = _draft(
        packages_breakdown=[
            {"type": "2-pak", "qty": 2},
            {"type": "szkło", "qty": 1},
        ]
    )

    specs = inpost_call_specs(draft, _SENDER)

    assert [
        (service, package_type, number, reference)
        for service, package_type, number, reference, _ in specs
    ] == [
        ("kurier", "2-pak", 1, "1700 | plastik | 2-pak 1/2"),
        ("kurier", "2-pak", 2, "1700 | plastik | 2-pak 2/2"),
        ("kurier", "szkło", 1, "1700 | szkło | 1-pak"),
    ]
    assert specs[0][4]["receiver_building_number"] == "7/2"
    assert specs[0][4]["sender"] is _SENDER
    assert specs[0][4]["dimensions"] is PARCEL_SPECS["2-pak"]


def test_call_specs_forward_cod_to_the_single_provider_shipment() -> None:
    specs = inpost_call_specs(
        _draft(cod={"amount": "200.30", "currency": "PLN"}),
        _SENDER,
    )

    assert specs[0][4]["cod_amount"] == "200.30"
    assert specs[0][4]["cod_currency"] == "PLN"


def test_multi_parcel_cod_is_rejected_before_payload_build() -> None:
    draft = _draft(
        cod={"amount": "500.00", "currency": "PLN"},
        packages_breakdown=[{"type": "1-pak", "qty": 2}],
    )

    with pytest.raises(InPostBusinessError, match="exactly one"):
        inpost_call_specs(draft, _SENDER)


def test_invalid_shopify_cod_data_is_rejected_even_after_manual_review() -> None:
    draft = _draft(cod=None, cod_error="COD order is missing Shopify total_outstanding")

    with pytest.raises(InPostBusinessError, match="total_outstanding"):
        inpost_call_specs(draft, _SENDER)


def test_pending_specs_filter_by_package_type_and_number() -> None:
    draft = _draft(
        packages_breakdown=[
            {"type": "1-pak", "qty": 2},
            {"type": "szkło", "qty": 1},
        ],
        courier_shipments=[
            {"package_type": "1-pak", "package_number": "1"},
            {"package_type": "szkło", "package_number": "1"},
        ],
    )

    specs = pending_inpost_call_specs(draft, _SENDER)

    assert [(spec[1], spec[2]) for spec in specs] == [("1-pak", 2)]


def test_pending_specs_accept_injected_call_specs() -> None:
    sentinel = [("kurier", "custom-box", 7, "patched", {"patched": True})]

    assert (
        pending_inpost_call_specs(
            _draft(),
            _SENDER,
            build_call_specs=lambda draft, sender: sentinel,
        )
        == sentinel
    )


def test_payload_plan_accepts_injected_pending_specs() -> None:
    builder = _RecordingPayloadBuilder()

    assert (
        inpost_payload_plan(
            _draft(),
            _SENDER,
            builder,
            build_pending_call_specs=lambda draft, sender: [],
        )
        == []
    )


@pytest.mark.parametrize(
    ("service", "expected_builder"),
    [
        ("inpost_courier_standard", "kurier"),
        ("inpost_locker_standard", "paczkomat"),
    ],
)
def test_payload_plan_uses_injected_builder_and_preserves_legacy_shape(
    service: str, expected_builder: str
) -> None:
    draft = _draft(service=service)
    builder = _RecordingPayloadBuilder()

    plan = inpost_payload_plan(draft, _SENDER, builder)

    assert plan == [
        {
            "service": service,
            "package_type": "1-pak",
            "package_number": 1,
            "reference": "1700 | plastik | 1-pak",
            "payload": {
                "kind": expected_builder,
                "reference": "1700 | plastik | 1-pak",
            },
        }
    ]
    assert len(builder.kurier_calls) == int(expected_builder == "kurier")
    assert len(builder.paczkomat_calls) == int(expected_builder == "paczkomat")


@pytest.mark.parametrize(
    ("status", "shipment_id", "expected"),
    [
        ("pending_confirmation", "ship-1", True),
        ("pending_confirmation", " ship-1 ", True),
        ("pending_confirmation", "", False),
        ("pending_confirmation", "   ", False),
        ("created", "ship-1", False),
        ("error", "ship-1", False),
    ],
)
def test_resumable_predicate_preserves_status_and_id_rules(
    status: str, shipment_id: str, expected: bool
) -> None:
    assert (
        is_resumable_inpost_draft({"status": status, "courier_draft_id": shipment_id}) is expected
    )


def test_resume_uses_existing_shipment_without_waiting_or_posting_again() -> None:
    client = MagicMock()
    client.get_shipment.return_value = {"id": "ship-1", "tracking_number": "TRACK-1"}
    draft = _draft(
        status="pending_confirmation",
        courier_draft_id="ship-1",
        courier_shipments=[
            {
                "id": "ship-1",
                "tracking_number": "",
                "package_type": "2-pak",
                "package_number": "1",
            },
            {
                "id": "ship-2",
                "tracking_number": "TRACK-2",
                "package_type": "2-pak",
                "package_number": "2",
            },
        ],
    )

    patch = resume_inpost_shipment(client, draft, build_patch=_shipment_patch)

    assert patch == {
        "courier_draft_id": "ship-1",
        "courier_shipments": [
            {
                "id": "ship-1",
                "tracking_number": "TRACK-1",
                "package_type": "2-pak",
                "package_number": "1",
            },
            {
                "id": "ship-2",
                "tracking_number": "TRACK-2",
                "package_type": "2-pak",
                "package_number": "2",
            },
        ],
        "dispatch_order_id": None,
        "tracking_number": "TRACK-1",
        "status": "created",
        "pickup_ordered": False,
        "error": None,
    }
    client.get_shipment.assert_called_once_with("ship-1")
    client.wait_for_shipment_confirmation.assert_not_called()
    client.create_kurier_shipment.assert_not_called()
    client.create_paczkomat_shipment.assert_not_called()


def test_resume_waits_three_times_and_preserves_pending_patch_without_tracking() -> None:
    client = MagicMock()
    client.get_shipment.return_value = {"id": "ship-legacy", "tracking_number": None}
    client.wait_for_shipment_confirmation.return_value = {
        "id": "ship-legacy",
        "tracking_number": None,
    }
    draft = _draft(
        status="pending_confirmation",
        courier_draft_id=" ship-legacy ",
        courier_shipments=[],
    )

    patch = resume_inpost_shipment(client, draft, build_patch=_shipment_patch)

    assert patch == {
        "courier_draft_id": "ship-legacy",
        "courier_shipments": [
            {
                "id": "ship-legacy",
                "tracking_number": "",
                "package_type": "1-pak",
                "package_number": "1",
            }
        ],
        "dispatch_order_id": None,
        "tracking_number": "",
        "status": "pending_confirmation",
        "pickup_ordered": False,
        "error": None,
    }
    client.get_shipment.assert_called_once_with("ship-legacy")
    client.wait_for_shipment_confirmation.assert_called_once_with(
        "ship-legacy",
        max_attempts=3,
        interval_s=1.0,
    )
    client.create_kurier_shipment.assert_not_called()
    client.create_paczkomat_shipment.assert_not_called()


def test_resume_refreshes_each_unconfirmed_parcel_without_reordering_or_losing_state() -> None:
    client = MagicMock()

    def get_shipment(shipment_id: str) -> dict[str, str]:
        assert shipment_id == "ship-2"
        return {"id": "ship-2", "tracking_number": "TRACK-2"}

    client.get_shipment.side_effect = get_shipment
    draft = _draft(
        status="pending_confirmation",
        courier_draft_id="ship-1",
        dispatch_order_id="dispatch-123",
        pickup_ordered=True,
        courier_shipments=[
            {
                "id": "ship-1",
                "tracking_number": "TRACK-1",
                "package_type": "1-pak",
                "package_number": "1",
                "label_format": "A6",
            },
            {
                "id": "ship-2",
                "tracking_number": "",
                "package_type": "1-pak",
                "package_number": "2",
                "label_format": "A6",
            },
        ],
    )

    patch = resume_inpost_shipment(client, draft, build_patch=_shipment_patch)

    assert patch["courier_shipments"] == [
        {
            "id": "ship-1",
            "tracking_number": "TRACK-1",
            "package_type": "1-pak",
            "package_number": "1",
            "label_format": "A6",
        },
        {
            "id": "ship-2",
            "tracking_number": "TRACK-2",
            "package_type": "1-pak",
            "package_number": "2",
            "label_format": "A6",
        },
    ]
    assert patch["status"] == "created"
    assert patch["dispatch_order_id"] == "dispatch-123"
    assert patch["pickup_ordered"] is True
    client.get_shipment.assert_called_once_with("ship-2")
    client.wait_for_shipment_confirmation.assert_not_called()
    client.create_kurier_shipment.assert_not_called()
    client.create_paczkomat_shipment.assert_not_called()


def test_resume_waits_only_for_the_unconfirmed_parcel_with_existing_policy() -> None:
    client = MagicMock()
    client.get_shipment.return_value = {"id": "ship-2", "tracking_number": None}
    client.wait_for_shipment_confirmation.return_value = {
        "id": "ship-2",
        "tracking_number": "TRACK-2",
    }
    draft = _draft(
        status="pending_confirmation",
        courier_draft_id="ship-1",
        courier_shipments=[
            {
                "id": "ship-1",
                "tracking_number": "TRACK-1",
                "package_type": "2-pak",
                "package_number": "1",
            },
            {
                "id": "ship-2",
                "tracking_number": "",
                "package_type": "2-pak",
                "package_number": "2",
            },
        ],
    )

    patch = resume_inpost_shipment(client, draft, build_patch=_shipment_patch)

    client.get_shipment.assert_called_once_with("ship-2")
    client.wait_for_shipment_confirmation.assert_called_once_with(
        "ship-2",
        max_attempts=3,
        interval_s=1.0,
    )
    assert [shipment["tracking_number"] for shipment in patch["courier_shipments"]] == [
        "TRACK-1",
        "TRACK-2",
    ]
    assert patch["status"] == "created"


def test_resume_keeps_complete_collection_when_one_parcel_stays_pending() -> None:
    client = MagicMock()
    client.get_shipment.return_value = {"id": "ship-2", "tracking_number": None}
    client.wait_for_shipment_confirmation.return_value = {
        "id": "ship-2",
        "tracking_number": None,
    }
    draft = _draft(
        status="pending_confirmation",
        courier_draft_id="ship-1",
        courier_shipments=[
            {
                "id": "ship-1",
                "tracking_number": "TRACK-1",
                "package_type": "szkło",
                "package_number": "1",
                "custom_metadata": "first",
            },
            {
                "id": "ship-2",
                "tracking_number": "",
                "package_type": "szkło",
                "package_number": "2",
                "custom_metadata": "second",
            },
        ],
    )

    patch = resume_inpost_shipment(client, draft, build_patch=_shipment_patch)

    assert patch["courier_shipments"] == draft["courier_shipments"]
    assert patch["status"] == "pending_confirmation"
    client.get_shipment.assert_called_once_with("ship-2")
    client.wait_for_shipment_confirmation.assert_called_once_with(
        "ship-2",
        max_attempts=3,
        interval_s=1.0,
    )
    client.create_kurier_shipment.assert_not_called()
    client.create_paczkomat_shipment.assert_not_called()


def test_historical_resume_preserves_existing_pickup_state() -> None:
    client = MagicMock()
    client.get_shipment.return_value = {"id": "legacy-ship", "tracking_number": "TRACK-1"}
    draft = _draft(
        status="pending_confirmation",
        courier_draft_id="legacy-ship",
        courier_shipments=[],
        dispatch_order_id="dispatch-legacy",
        pickup_ordered=True,
    )

    patch = resume_inpost_shipment(client, draft, build_patch=_shipment_patch)

    assert patch["courier_shipments"] == [
        {
            "id": "legacy-ship",
            "tracking_number": "TRACK-1",
            "package_type": "1-pak",
            "package_number": "1",
        }
    ]
    assert patch["dispatch_order_id"] == "dispatch-legacy"
    assert patch["pickup_ordered"] is True
    client.get_shipment.assert_called_once_with("legacy-ship")
    client.wait_for_shipment_confirmation.assert_not_called()


@pytest.mark.parametrize(
    ("courier_draft_id", "courier_shipments", "remote_tracking"),
    [
        ("legacy-ship", [], {"legacy-ship": "TRACK-1"}),
        (
            "ship-1",
            [
                {
                    "id": "ship-1",
                    "tracking_number": "",
                    "package_type": "1-pak",
                    "package_number": "1",
                }
            ],
            {"ship-1": "TRACK-1"},
        ),
        (
            "ship-1",
            [
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
            {"ship-2": "TRACK-2"},
        ),
        (
            "ship-1",
            [
                {
                    "id": "ship-1",
                    "tracking_number": "TRACK-1",
                    "package_type": "1-pak",
                    "package_number": "1",
                },
                {
                    "id": "ship-2",
                    "tracking_number": "TRACK-2",
                    "package_type": "1-pak",
                    "package_number": "2",
                },
            ],
            {},
        ),
    ],
    ids=("historical", "single", "partially-confirmed", "fully-confirmed"),
)
def test_resume_never_posts_another_paid_shipment(
    courier_draft_id: str,
    courier_shipments: list[dict[str, str]],
    remote_tracking: dict[str, str],
) -> None:
    client = MagicMock()
    client.get_shipment.side_effect = lambda shipment_id: {
        "id": shipment_id,
        "tracking_number": remote_tracking.get(shipment_id),
    }
    client.wait_for_shipment_confirmation.side_effect = lambda shipment_id, **kwargs: {
        "id": shipment_id,
        "tracking_number": remote_tracking.get(shipment_id),
    }
    draft = _draft(
        status="pending_confirmation",
        courier_draft_id=courier_draft_id,
        courier_shipments=courier_shipments,
    )

    resume_inpost_shipment(client, draft, build_patch=_shipment_patch)

    client.create_kurier_shipment.assert_not_called()
    client.create_paczkomat_shipment.assert_not_called()
    client._post_shipment.assert_not_called()
