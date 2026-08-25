"""Direct tests for HTTP-neutral Apaczka shipment planning."""

from __future__ import annotations

from typing import Any

import pytest

from zdrovena.common.apaczka import APACZKA_CONTENT_MAX_LENGTH
from zdrovena.common.shipping_exceptions import ApaczkaBusinessError
from zdrovena.shipping.providers.apaczka import (
    apaczka_call_specs,
    apaczka_payload_plan,
    validate_apaczka_cod_pickup_point,
)

_PICKUP_ADDRESS = {
    "name": "Zdrovena Magazyn",
    "firstname": "",
    "lastname": "Zdrovena",
    "email": "magazyn@example.test",
    "phone": "500000000",
    "street": "Naściszowa",
    "building_number": "41",
    "city": "Naściszowa",
    "post_code": "33-300",
}


def _draft(**overrides: Any) -> dict[str, Any]:
    draft: dict[str, Any] = {
        "id": "apaczka-provider-test",
        "shopify_order_number": "1800",
        "courier": "apaczka",
        "service": "apaczka",
        "apaczka_service_id": "23",
        "receiver": {
            "first_name": "Anna",
            "last_name": "Nowak",
            "email": "anna@example.test",
            "phone": "600100200",
            "locker_id": "",
        },
        "shipping_address": {
            "street": "Kwiatowa",
            "building_number": "7",
            "flat_number": "2",
            "city": "Warszawa",
            "post_code": "00-001",
        },
        "order_items": [{"name": "HUMIO 500 ml", "quantity": 2}],
        "packages_breakdown": [{"type": "1-pak", "qty": 1}],
        "courier_shipments": [],
    }
    draft.update(overrides)
    return draft


class _RecordingPayloadBuilder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def build_shipment_order(
        self,
        *,
        receiver_name: str,
        receiver_firstname: str,
        receiver_lastname: str,
        receiver_email: str,
        receiver_phone: str,
        receiver_address: str,
        receiver_city: str,
        receiver_zip: str,
        receiver_point_id: str | None = None,
        sender: dict[str, str],
        reference: str,
        content: str,
        weight_kg: float = 1.0,
        width_cm: float = 20.0,
        height_cm: float = 15.0,
        depth_cm: float = 30.0,
        pickup_date: str | None = None,
        pickup_from: str | None = None,
        pickup_to: str | None = None,
    ) -> dict[str, Any]:
        call = {
            "receiver_name": receiver_name,
            "receiver_firstname": receiver_firstname,
            "receiver_lastname": receiver_lastname,
            "receiver_email": receiver_email,
            "receiver_phone": receiver_phone,
            "receiver_address": receiver_address,
            "receiver_city": receiver_city,
            "receiver_zip": receiver_zip,
            "receiver_point_id": receiver_point_id,
            "sender": sender,
            "reference": reference,
            "content": content,
            "weight_kg": weight_kg,
            "width_cm": width_cm,
            "height_cm": height_cm,
            "depth_cm": depth_cm,
            "pickup_date": pickup_date,
            "pickup_from": pickup_from,
            "pickup_to": pickup_to,
        }
        self.calls.append(call)
        pickup: dict[str, Any] = {"type": "COURIER"}
        if pickup_date:
            pickup["date"] = pickup_date
            if pickup_from:
                pickup["hours_from"] = pickup_from
            if pickup_to:
                pickup["hours_to"] = pickup_to
        return {
            "address": {"sender": sender},
            "externalId": reference,
            "pickup": pickup,
        }


class _PointLookup:
    def __init__(self, point: dict[str, Any] | None) -> None:
        self.point = point
        self.calls: list[tuple[str, str]] = []

    def get_point(self, point_type: str, foreign_address_id: str) -> dict[str, Any] | None:
        self.calls.append((point_type, foreign_address_id))
        return self.point


def test_call_specs_preserve_order_and_per_type_numbering() -> None:
    draft = _draft(
        packages_breakdown=[
            {"type": "2-pak", "qty": 2},
            {"type": "szkło", "qty": 1},
            {"type": "3-pak", "qty": 2},
        ]
    )

    specs = apaczka_call_specs(draft, _PICKUP_ADDRESS)

    assert [
        (spec["package_type"], spec["package_number"], spec["kwargs"]["reference"])
        for spec in specs
    ] == [
        ("2-pak", 1, "1800 | plastik | 2-pak 1/2"),
        ("2-pak", 2, "1800 | plastik | 2-pak 2/2"),
        ("szkło", 1, "1800 | szkło | 1-pak"),
        ("3-pak", 1, "1800 | plastik | 3-pak 1/2"),
        ("3-pak", 2, "1800 | plastik | 3-pak 2/2"),
    ]


def test_content_describes_the_parcel_and_never_the_product_names() -> None:
    # The catalogue rename that broke production pushed the old product-name
    # content ("2 x HUMIO - Alkaliczna Woda Humusowa w szkle 500ml x 12") to 55
    # characters and Apaczka rejected the whole order with a 400.
    draft = _draft(
        order_items=[
            {"name": "HUMIO - Alkaliczna Woda Humusowa w szkle 500ml x 12", "quantity": 2}
        ],
        packages_breakdown=[{"type": "szkło", "qty": 1}, {"type": "3-pak", "qty": 1}],
    )

    specs = apaczka_call_specs(draft, _PICKUP_ADDRESS)

    contents = [spec["kwargs"]["content"] for spec in specs]
    assert contents == ["Woda butelkowana, szkło 1-pak", "Woda butelkowana, plastik 3-pak"]
    assert all(len(content) <= APACZKA_CONTENT_MAX_LENGTH for content in contents)


def test_content_survives_an_empty_or_unreadable_product_list() -> None:
    draft = _draft(order_items=[], packages_breakdown=[{"type": "1-pak", "qty": 1}])

    specs = apaczka_call_specs(draft, _PICKUP_ADDRESS)

    assert specs[0]["kwargs"]["content"] == "Woda butelkowana, plastik 1-pak"


def test_call_specs_forward_cod_and_bank_account() -> None:
    specs = apaczka_call_specs(
        _draft(cod={"amount": "200.30", "currency": "PLN"}),
        _PICKUP_ADDRESS,
        cod_bank_account="12345678901234567890123456",
    )

    assert specs[0]["kwargs"]["cod_amount"] == "200.30"
    assert specs[0]["kwargs"]["cod_currency"] == "PLN"
    assert specs[0]["kwargs"]["cod_bank_account"] == "12345678901234567890123456"


def test_cod_pickup_point_must_explicitly_advertise_cod() -> None:
    lookup = _PointLookup({"foreign_address_id": "PL55338", "option_cod": False})
    draft = _draft(
        cod={"amount": "200.30", "currency": "PLN"},
        pickup_point={"provider": "dpd", "id": "PL55338"},
    )

    with pytest.raises(ApaczkaBusinessError, match="does not support COD"):
        validate_apaczka_cod_pickup_point(draft, lookup)

    assert lookup.calls == [("DPD", "PL55338")]


def test_cod_pickup_point_passes_only_on_literal_true_capability() -> None:
    lookup = _PointLookup({"foreign_address_id": "PL55338", "option_cod": True})
    draft = _draft(
        cod={"amount": "200.30", "currency": "PLN"},
        pickup_point={"provider": "dpd", "id": "PL55338"},
    )

    validate_apaczka_cod_pickup_point(draft, lookup)

    assert lookup.calls == [("DPD", "PL55338")]


@pytest.mark.parametrize("point", [None, {"foreign_address_id": "PL55338"}])
def test_cod_pickup_point_fails_closed_when_capability_is_unavailable(
    point: dict[str, Any] | None,
) -> None:
    lookup = _PointLookup(point)
    draft = _draft(
        cod={"amount": "200.30", "currency": "PLN"},
        pickup_point={"provider": "dpd", "id": "PL55338"},
    )

    with pytest.raises(ApaczkaBusinessError):
        validate_apaczka_cod_pickup_point(draft, lookup)


def test_cod_pickup_point_fails_closed_for_unknown_point_service() -> None:
    lookup = _PointLookup({"foreign_address_id": "UNKNOWN", "option_cod": True})
    draft = _draft(
        apaczka_service_id="999",
        cod={"amount": "200.30", "currency": "PLN"},
        pickup_point={"provider": "unknown", "id": "UNKNOWN"},
    )

    with pytest.raises(ApaczkaBusinessError, match="no documented point type"):
        validate_apaczka_cod_pickup_point(draft, lookup)

    assert lookup.calls == []


def test_non_cod_pickup_point_does_not_require_capability_lookup() -> None:
    lookup = _PointLookup(None)

    validate_apaczka_cod_pickup_point(
        _draft(pickup_point={"provider": "dpd", "id": "PL55338"}), lookup
    )

    assert lookup.calls == []


def test_multi_parcel_cod_is_rejected_before_payload_build() -> None:
    draft = _draft(
        cod={"amount": "500.00", "currency": "PLN"},
        packages_breakdown=[{"type": "1-pak", "qty": 2}],
    )

    with pytest.raises(ApaczkaBusinessError, match="exactly one"):
        apaczka_call_specs(
            draft,
            _PICKUP_ADDRESS,
            cod_bank_account="12345678901234567890123456",
        )


def test_invalid_shopify_cod_data_is_rejected_even_after_manual_review() -> None:
    draft = _draft(cod=None, cod_error="Unsupported COD currency: EUR")

    with pytest.raises(ApaczkaBusinessError, match="Unsupported COD currency"):
        apaczka_call_specs(draft, _PICKUP_ADDRESS)


def test_call_specs_filter_completed_parcels_by_type_and_number() -> None:
    draft = _draft(
        packages_breakdown=[
            {"type": "1-pak", "qty": 2},
            {"type": "szkło", "qty": 1},
        ],
        courier_shipments=[
            {"id": "ap-1", "package_type": "1-pak", "package_number": "1"},
            {"id": "ap-2", "package_type": "szkło", "package_number": "1"},
        ],
    )

    specs = apaczka_call_specs(draft, _PICKUP_ADDRESS)

    assert [(spec["package_type"], spec["package_number"]) for spec in specs] == [("1-pak", 2)]


def test_payload_plan_uses_injected_builder_and_keeps_pickup_address_as_sender() -> None:
    builder = _RecordingPayloadBuilder()

    plan = apaczka_payload_plan(_draft(), _PICKUP_ADDRESS, builder)

    assert builder.calls[0]["sender"] is _PICKUP_ADDRESS
    assert plan[0]["payload"]["address"]["sender"] is _PICKUP_ADDRESS


@pytest.mark.parametrize(
    ("pickup_from", "pickup_to"),
    [
        ("09:00", "17:00"),
        ("11:00", "14:00"),
        ("14:00", "17:00"),
    ],
)
def test_execution_call_specs_preserve_supported_pickup_windows(
    pickup_from: str, pickup_to: str
) -> None:
    specs = apaczka_call_specs(
        _draft(),
        _PICKUP_ADDRESS,
        pickup_date="2026-08-12",
        pickup_from=pickup_from,
        pickup_to=pickup_to,
    )

    assert {
        field: specs[0]["kwargs"][field] for field in ("pickup_date", "pickup_from", "pickup_to")
    } == {
        "pickup_date": "2026-08-12",
        "pickup_from": pickup_from,
        "pickup_to": pickup_to,
    }


def test_unsupported_production_like_window_is_rejected_before_payload_build() -> None:
    builder = _RecordingPayloadBuilder()

    with pytest.raises(ApaczkaBusinessError, match=r"09:00.17:00"):
        apaczka_payload_plan(
            _draft(),
            _PICKUP_ADDRESS,
            builder,
            pickup_date="2026-08-12",
            pickup_from="11:00",
            pickup_to="13:00",
        )

    assert builder.calls == []


def test_unverified_service_keeps_its_existing_pickup_window_semantics() -> None:
    specs = apaczka_call_specs(
        _draft(apaczka_service_id="21"),
        _PICKUP_ADDRESS,
        pickup_date="2026-08-12",
        pickup_from="10:00",
        pickup_to="13:00",
    )

    assert specs[0]["kwargs"]["pickup_from"] == "10:00"
    assert specs[0]["kwargs"]["pickup_to"] == "13:00"


def test_preview_payload_plan_matches_execute_time_pickup_schedule() -> None:
    builder = _RecordingPayloadBuilder()

    plan = apaczka_payload_plan(
        _draft(),
        _PICKUP_ADDRESS,
        builder,
        pickup_date="2026-08-12",
        pickup_from="11:00",
        pickup_to="14:00",
    )

    assert plan[0]["payload"]["pickup"] == {
        "type": "COURIER",
        "date": "2026-08-12",
        "hours_from": "11:00",
        "hours_to": "14:00",
    }
    assert {
        field: builder.calls[0][field] for field in ("pickup_date", "pickup_from", "pickup_to")
    } == {
        "pickup_date": "2026-08-12",
        "pickup_from": "11:00",
        "pickup_to": "14:00",
    }


def test_planning_uses_the_canonical_mutable_parcel_catalog(monkeypatch) -> None:
    from zdrovena.common import inpost
    from zdrovena.common.shipping_parcels import PARCEL_SPECS

    assert inpost.PARCEL_SPECS is PARCEL_SPECS
    monkeypatch.setitem(
        inpost.PARCEL_SPECS,
        "custom-box",
        {
            "length": 51,
            "width": 41,
            "height": 31,
            "weight_kg": 7.5,
            "paczkomat_template": "large",
        },
    )

    specs = apaczka_call_specs(
        _draft(packages_breakdown=[{"type": "custom-box", "qty": 1}]),
        _PICKUP_ADDRESS,
    )

    assert {
        field: specs[0]["kwargs"][field]
        for field in ("weight_kg", "width_cm", "height_cm", "depth_cm")
    } == {
        "weight_kg": 7.5,
        "width_cm": 41,
        "height_cm": 31,
        "depth_cm": 51,
    }


def test_router_preview_composes_apaczka_provider_planner(monkeypatch) -> None:
    from zdrovena.api import shipping_execution_composition as webhooks
    from zdrovena.common.apaczka import ApaczkaClient

    draft = _draft()
    parcels = [{"payload": {"externalId": "1800 | plastik | 1-pak"}}]
    captured: dict[str, Any] = {}

    def fake_payload_plan(
        planned_draft: dict[str, Any],
        pickup_address: dict[str, str],
        builder: Any,
        **pickup_schedule: Any,
    ) -> list[dict[str, Any]]:
        captured.update(
            draft=planned_draft,
            pickup_address=pickup_address,
            builder=builder,
            pickup_schedule=pickup_schedule,
        )
        return parcels

    monkeypatch.setattr(webhooks, "get_pickup_address", lambda: _PICKUP_ADDRESS)
    monkeypatch.setattr(webhooks.apaczka_provider, "apaczka_payload_plan", fake_payload_plan)

    preview = webhooks.execution_preview(draft)

    assert preview["courier"] == "apaczka"
    assert preview["sender"] is _PICKUP_ADDRESS
    assert preview["parcels"] is parcels
    assert preview["preview_available"] is True
    assert captured["draft"] is draft
    assert captured["pickup_address"] is _PICKUP_ADDRESS
    assert isinstance(captured["builder"], ApaczkaClient)
