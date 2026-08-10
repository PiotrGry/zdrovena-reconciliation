"""Direct tests for HTTP-neutral InPost planning and resume behavior."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

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


def test_legacy_pending_wrapper_honors_monkeypatched_call_specs(monkeypatch) -> None:
    from zdrovena.api.routers import webhooks

    sentinel = [("kurier", "custom-box", 7, "patched", {"patched": True})]
    monkeypatch.setattr(webhooks, "_inpost_call_specs", lambda draft, sender: sentinel)

    assert webhooks._pending_inpost_call_specs(_draft(), _SENDER) == sentinel


def test_legacy_payload_wrapper_honors_monkeypatched_pending_specs(monkeypatch) -> None:
    from zdrovena.api.routers import webhooks

    monkeypatch.setattr(webhooks, "_pending_inpost_call_specs", lambda draft, sender: [])

    assert webhooks._inpost_payload_plan(_draft(), _SENDER) == []


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
