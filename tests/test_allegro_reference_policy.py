"""Regression tests for #462: Allegro referenceNumber / textOnLabel policy.

Production rejected ``input.referenceNumber`` with VALIDATION_ERROR twice
(2026-09-15 and 2026-10-26). The rejected values were the portal packing
annotation built on the Allegro checkout UUID, e.g.
``"<checkout UUID> | plastik | 3-pak"``. Allegro accepts only letters without
diacritics, digits and ``_/-`` there.

No real provider is contacted: HTTP goes to a patched ``requests`` session.
"""

from __future__ import annotations

import json
import re
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from zdrovena.common.allegro import AllegroClient
from zdrovena.common.allegro_reference import (
    ALLEGRO_REFERENCE_MAX_LENGTH,
    ALLEGRO_TEXT_ON_LABEL_MAX_LENGTH,
    allegro_reference_number,
    allegro_text_on_label,
    fold_to_ascii,
)
from zdrovena.common.shipping_exceptions import AllegroBusinessError
from zdrovena.shipping.domain.planning import physical_parcels, shipment_reference
from zdrovena.shipping.providers.allegro_delivery import (
    allegro_call_specs,
    allegro_payload_plan,
)
from zdrovena.shipping.providers.inpost import inpost_call_specs

# What Allegro production said it accepts (letters w/o diacritics, digits, _/-).
PRODUCTION_ALLOWED = re.compile(r"^[A-Za-z0-9_/-]+$")

# UUID-shaped Allegro checkout ids, one per production failure. The real ids
# are not recorded in the issue, so these keep the shape (8-4-4-4-12 hex).
FAILURE_2026_09_15_ORDER = "1c5e0b30-94db-11f1-82ad-f5cfa7ea889b"
FAILURE_2026_10_26_ORDER = "9f2a6c1e-3b7d-4e8a-b5c4-0d1e2f3a4b5c"

_PROPOSAL: dict[str, Any] = {
    "suggestedInput": {
        "sender": {
            "name": "Zdrovena",
            "street": "Cieszyńska 6",
            "postalCode": "30-015",
            "city": "Kraków",
            "countryCode": "PL",
            "email": "sender@example.test",
            "phone": "500600700",
        },
        "receiver": {
            "name": "Ola Wiśniewska",
            "street": "Lipowa 3",
            "postalCode": "90-001",
            "city": "Łódź",
            "countryCode": "PL",
            "email": "masked+buyer@allegromail.pl",
            "phone": "600100200",
        },
    }
}


class _ProposalOnly:
    def __init__(self) -> None:
        self.writes: list[str] = []

    def get_delivery_proposal(self, order_id: str) -> dict[str, Any]:
        return _PROPOSAL


def _draft(order_number: str, breakdown: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": "draft-462",
        "source": "allegro",
        "shopify_order_number": order_number,
        "external_order_id": order_number,
        "courier": "allegro_delivery",
        "service": "allegro_delivery",
        "receiver": {"locker_id": "LOD01A", "phone": "600100200"},
        "shipping_address": {"street": "Lipowa", "building_number": "3", "city": "Lodz"},
        "packages_breakdown": breakdown,
        "allegro_delivery_method_id": None,
        "allegro_credentials_id": None,
    }


def _client() -> AllegroClient:
    client = AllegroClient(client_id="cid", client_secret="cs", refresh_token="rt", env="prod")
    client._access_token = "test-token"
    client._expires_at = 9999999999
    return client


def _response(status: int, payload: dict[str, Any]) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.text = json.dumps(payload)
    resp.json.return_value = payload
    return resp


def _serialized_bodies(draft: dict[str, Any]) -> list[dict[str, Any]]:
    """Send every planned create-command through AllegroClient; return wire bodies."""
    client = _client()
    bodies: list[dict[str, Any]] = []
    specs = allegro_call_specs(draft, _PROPOSAL)
    with patch.object(
        client._session, "request", return_value=_response(201, {"commandId": "c"})
    ) as request:
        for index, spec in enumerate(specs):
            client.create_ship_with_allegro_shipment(command_id=f"cmd-{index}", **spec["kwargs"])
    for call in request.call_args_list:
        # Round-trip through JSON exactly as `requests` would serialize it.
        bodies.append(json.loads(json.dumps(call.kwargs["json"])))
    return bodies


# ── Production-shaped failures ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("order_number", "breakdown", "old_rejected_reference"),
    [
        (
            FAILURE_2026_09_15_ORDER,
            [{"type": "szkło", "qty": 1}],
            f"{FAILURE_2026_09_15_ORDER} | szkło | 1-pak",
        ),
        (
            FAILURE_2026_10_26_ORDER,
            [{"type": "3-pak", "qty": 1}],
            f"{FAILURE_2026_10_26_ORDER} | plastik | 3-pak",
        ),
    ],
)
def test_production_failures_now_produce_valid_allegro_reference(
    order_number: str, breakdown: list[dict[str, Any]], old_rejected_reference: str
) -> None:
    draft = _draft(order_number, breakdown)

    # The old wire value really is outside what Allegro accepts.
    assert not PRODUCTION_ALLOWED.fullmatch(old_rejected_reference)
    # The portal annotation is unchanged and still equals what was rejected.
    assert shipment_reference(order_number, breakdown[0]["type"], 1, 1) == old_rejected_reference

    (body,) = _serialized_bodies(draft)
    reference = body["input"]["referenceNumber"]
    assert PRODUCTION_ALLOWED.fullmatch(reference)
    assert reference == order_number.replace("-", "")
    assert len(reference) == 32 <= ALLEGRO_REFERENCE_MAX_LENGTH


def test_uuid_reference_is_lossless() -> None:
    reference = allegro_reference_number(FAILURE_2026_10_26_ORDER)
    restored = "-".join(
        (reference[:8], reference[8:12], reference[12:16], reference[16:20], reference[20:])
    )
    assert restored == FAILURE_2026_10_26_ORDER


# ── textOnLabel carries material, pack size and parcel identification ────────


def test_glass_and_diacritics_move_to_ascii_text_on_label() -> None:
    draft = _draft(FAILURE_2026_09_15_ORDER, [{"type": "szkło", "qty": 1}])

    (body,) = _serialized_bodies(draft)

    assert body["input"]["packages"][0]["textOnLabel"] == "szklo 1-pak"
    plan = allegro_payload_plan(draft, _ProposalOnly())
    assert plan[0]["annotation"] == f"{FAILURE_2026_09_15_ORDER} | szkło | 1-pak"


def test_repeated_and_mixed_physical_boxes_are_distinguishable_on_labels() -> None:
    breakdown = [
        {"type": "3-pak", "qty": 2},
        {"type": "pół-pak", "qty": 1},
        {"type": "szkło", "qty": 3},
        # Suspended legacy row: always two separate glass boxes.
        {"type": "szkło-2pak", "qty": 1},
    ]
    draft = _draft(FAILURE_2026_10_26_ORDER, breakdown)

    bodies = _serialized_bodies(draft)
    labels = [body["input"]["packages"][0]["textOnLabel"] for body in bodies]
    plan = allegro_payload_plan(draft, _ProposalOnly())

    assert labels == [
        "plastik 3-pak 1/2",
        "plastik 3-pak 2/2",
        "plastik pol-pak",
        "szklo 1-pak 1/3",
        "szklo 1-pak 2/3",
        "szklo 1-pak 3/3",
        # physical_parcels restarts position per breakdown row; identity is
        # (package_type, package_number), unchanged from before #462.
        "szklo 1-pak 1/2",
        "szklo 1-pak 2/2",
    ]
    assert [(p["package_type"], p["package_number"]) for p in plan] == [
        (parcel.package_type, parcel.position) for parcel in physical_parcels(draft)
    ]
    assert {body["input"]["referenceNumber"] for body in bodies} == {
        FAILURE_2026_10_26_ORDER.replace("-", "")
    }
    assert all(len(label) <= ALLEGRO_TEXT_ON_LABEL_MAX_LENGTH for label in labels)
    # Annotation in the portal keeps the Polish wording and n/count.
    assert [p["annotation"].split(" | ", 1)[1] for p in plan] == [
        "plastik | 3-pak 1/2",
        "plastik | 3-pak 2/2",
        "plastik | pół-pak",
        "szkło | 1-pak 1/3",
        "szkło | 1-pak 2/3",
        "szkło | 1-pak 3/3",
        "szkło | 1-pak 1/2",
        "szkło | 1-pak 2/2",
    ]


# ── Determinism and preview/HTTP agreement ───────────────────────────────────


def test_replay_is_deterministic() -> None:
    draft = _draft(FAILURE_2026_10_26_ORDER, [{"type": "2-pak", "qty": 2}])

    # Same draft, same command ids → byte-identical wire bodies.
    assert json.dumps(_serialized_bodies(draft)) == json.dumps(_serialized_bodies(draft))
    assert allegro_payload_plan(draft, _ProposalOnly()) == allegro_payload_plan(
        draft, _ProposalOnly()
    )


def test_preview_and_serialized_http_body_agree() -> None:
    draft = _draft(
        FAILURE_2026_09_15_ORDER,
        [{"type": "2-pak", "qty": 2}, {"type": "szkło", "qty": 1}],
    )

    plan = allegro_payload_plan(draft, _ProposalOnly())
    bodies = _serialized_bodies(draft)

    assert len(plan) == len(bodies) == 3
    for entry, body in zip(plan, bodies, strict=True):
        assert body["input"]["referenceNumber"] == entry["reference"]
        assert body["input"]["referenceNumber"] == entry["payload"]["reference_number"]
        assert [p["textOnLabel"] for p in body["input"]["packages"]] == [entry["text_on_label"]]
        assert body["input"]["packages"] == entry["payload"]["packages"]


# ── Field limits and fail-closed behaviour ───────────────────────────────────


@pytest.mark.parametrize(
    ("order_number", "expected"),
    [
        ("1700", "1700"),
        ("#1700", "1700"),
        ("ALG-1", "ALG-1"),
        ("Zamówienie łódź 7", "Zamowienielodz7"),
        ("A" * ALLEGRO_REFERENCE_MAX_LENGTH, "A" * ALLEGRO_REFERENCE_MAX_LENGTH),
    ],
)
def test_reference_normalisation(order_number: str, expected: str) -> None:
    assert allegro_reference_number(order_number) == expected


@pytest.mark.parametrize(
    "order_number",
    [
        "",
        "|| ###",
        "A" * (ALLEGRO_REFERENCE_MAX_LENGTH + 1),
        # Not UUID-shaped, so hyphens stay and it does not fit: never truncated.
        "order-" + "1c5e0b30-94db-11f1-82ad-f5cfa7ea889b",
    ],
)
def test_reference_that_cannot_fit_fails_closed(order_number: str) -> None:
    with pytest.raises(AllegroBusinessError, match="referenceNumber"):
        allegro_reference_number(order_number)


def test_planner_fails_closed_before_any_http_call() -> None:
    draft = _draft("A" * (ALLEGRO_REFERENCE_MAX_LENGTH + 1), [{"type": "1-pak", "qty": 1}])

    with pytest.raises(AllegroBusinessError, match="never truncated"):
        allegro_call_specs(draft, _PROPOSAL)


def test_text_on_label_limit_fails_closed_instead_of_dropping_packing_info() -> None:
    longest_catalogue_label = allegro_text_on_label("plastik", "pół-pak", 99, 99)
    assert longest_catalogue_label == "plastik pol-pak 99/99"
    assert len(longest_catalogue_label) <= ALLEGRO_TEXT_ON_LABEL_MAX_LENGTH
    with pytest.raises(AllegroBusinessError, match="textOnLabel"):
        allegro_text_on_label("plastik", "x" * ALLEGRO_TEXT_ON_LABEL_MAX_LENGTH, 1, 1)


@pytest.mark.parametrize(
    ("reference", "package_extra"),
    [
        (f"{FAILURE_2026_10_26_ORDER} | plastik | 3-pak", {}),
        ("1700", {"textOnLabel": "szkło 1-pak"}),
        ("1700", {"textOnLabel": "x" * (ALLEGRO_TEXT_ON_LABEL_MAX_LENGTH + 1)}),
    ],
)
def test_client_refuses_invalid_fields_without_posting(
    reference: str, package_extra: dict[str, Any]
) -> None:
    client = _client()
    spec = allegro_call_specs(_draft("1700", [{"type": "1-pak", "qty": 1}]), _PROPOSAL)[0]
    kwargs = {**spec["kwargs"], "reference_number": reference}
    kwargs["packages"] = [{**kwargs["packages"][0], **package_extra}]

    with patch.object(client._session, "request") as request:
        with pytest.raises(AllegroBusinessError):
            client.create_ship_with_allegro_shipment(command_id="cmd", **kwargs)

    request.assert_not_called()


def test_fold_to_ascii_covers_polish_letters() -> None:
    assert fold_to_ascii("ąćęłńóśźż ĄĆĘŁŃÓŚŹŻ") == "acelnoszz ACELNOSZZ"


# ── Other couriers keep their behaviour ──────────────────────────────────────


def test_inpost_reference_is_unchanged_for_the_same_uuid_order() -> None:
    draft = _draft(FAILURE_2026_10_26_ORDER, [{"type": "3-pak", "qty": 2}])

    references = [spec[3] for spec in inpost_call_specs(draft, sender={})]

    assert references == [
        f"{FAILURE_2026_10_26_ORDER} | plastik | 3-pak 1/2",
        f"{FAILURE_2026_10_26_ORDER} | plastik | 3-pak 2/2",
    ]
