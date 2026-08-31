"""Tests for splitting one COD amount across the physical parcels of a draft.

The invariant that matters most: whatever the split, the parts must add up to
the amount Shopify says the customer still owes, to the grosz. A split that
loses or invents a grosz charges the wrong person the wrong money.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from zdrovena.common.shipping_parcels import PARCEL_HALF_PACKS, PARCEL_SPECS
from zdrovena.shipping.domain.cod import CodAllocationError, cod_allocation
from zdrovena.shipping.domain.planning import calc_packages

_PLASTIC = "HUMIO - Alkaliczna Woda Humusowa 500ml x 12"
_GLASS = "HUMIO - Alkaliczna Woda Humusowa w szkle 500ml x 12"


def _draft(
    *,
    amount: str,
    breakdown: list[dict],
    items: list[dict] | None = None,
    shipping_price: str | None = "0.00",
) -> dict:
    draft: dict = {
        "cod": {"amount": amount, "currency": "PLN"},
        "packages_breakdown": breakdown,
        "order_items": items if items is not None else [],
    }
    if shipping_price is not None:
        draft["shipping_price"] = shipping_price
    return draft


def _amounts(draft: dict) -> list[str]:
    return [str(value) for value in cod_allocation(draft).amounts]


class TestSumIsExact:
    """Σ of the parts equals the collected total, always."""

    def test_single_parcel_keeps_the_whole_amount(self):
        draft = _draft(
            amount="150.00",
            breakdown=[{"type": "1-pak", "qty": 1}],
            items=[{"name": _PLASTIC, "quantity": 1, "line_total": "150.00"}],
        )
        assert _amounts(draft) == ["150.00"]

    def test_order_1731_splits_evenly_across_two_identical_parcels(self):
        # The order that made the operator report this: Shopify #1731,
        # 351.00 PLN over "3-pak" x2, a single 72-bottle line.
        draft = _draft(
            amount="351.00",
            breakdown=[{"type": "3-pak", "qty": 2}],
            items=[
                {
                    "name": "Imprezowy zapas wody HUMIO – 72 butelki",
                    "quantity": 1,
                    "line_total": "351.00",
                }
            ],
        )
        assert _amounts(draft) == ["175.50", "175.50"]

    def test_shipping_is_shared_equally_and_goods_by_value(self):
        draft = _draft(
            amount="150.00",
            shipping_price="30.00",
            breakdown=[{"type": "3-pak", "qty": 2}],
            items=[
                {"name": "HUMIO 72 butelki", "quantity": 1, "line_total": "120.00"},
            ],
        )
        assert _amounts(draft) == ["75.00", "75.00"]

    def test_indivisible_shipping_puts_the_spare_grosz_on_the_first_parcel(self):
        draft = _draft(
            amount="10.00",
            shipping_price="10.00",
            breakdown=[{"type": "1-pak", "qty": 3}],
            items=[{"name": "HUMIO 36 butelek", "quantity": 1, "line_total": "0.00"}],
        )
        assert _amounts(draft) == ["3.34", "3.33", "3.33"]

    def test_indivisible_goods_spread_the_spare_grosze_over_earliest_parcels(self):
        draft = _draft(
            amount="100.01",
            breakdown=[{"type": "1-pak", "qty": 3}],
            items=[{"name": "HUMIO 36 butelek", "quantity": 1, "line_total": "100.01"}],
        )
        amounts = _amounts(draft)
        assert amounts == ["33.34", "33.34", "33.33"]
        assert sum(Decimal(value) for value in amounts) == Decimal("100.01")


class TestGoodsFollowTheParcelTheyAreIn:
    def test_a_bigger_parcel_carries_a_bigger_share(self):
        # 7 half-packs of goods: "3-pak" holds 6, "pół-pak" holds 1.
        draft = _draft(
            amount="700.00",
            breakdown=[{"type": "3-pak", "qty": 1}, {"type": "pół-pak", "qty": 1}],
            items=[{"name": "HUMIO 42 butelki", "quantity": 1, "line_total": "700.00"}],
        )
        assert _amounts(draft) == ["600.00", "100.00"]

    def test_glass_value_never_lands_in_a_plastic_parcel(self):
        # The parcel layout of production draft 1f2f5dfd: plastic and glass
        # bought together are packed into separate boxes, so their money must
        # be separated the same way.
        draft = _draft(
            amount="900.00",
            breakdown=[
                {"type": "3-pak", "qty": 1},
                {"type": "szkło-2pak", "qty": 1},
                {"type": "szkło", "qty": 1},
            ],
            items=[
                {"name": _PLASTIC, "quantity": 3, "line_total": "300.00"},
                {"name": _GLASS, "quantity": 3, "line_total": "600.00"},
            ],
        )
        assert _amounts(draft) == ["300.00", "400.00", "200.00"]

    def test_a_partly_empty_glass_parcel_is_normal(self):
        # calc_packages rounds glass up: 5 half-packs of glass become 3 packs
        # of capacity, so the last box is half empty by design, not by error.
        draft = _draft(
            amount="600.00",
            breakdown=[{"type": "szkło-2pak", "qty": 1}, {"type": "szkło", "qty": 1}],
            items=[{"name": "Woda w szkle 30 butelek", "quantity": 1, "line_total": "600.00"}],
        )
        assert _amounts(draft) == ["480.00", "120.00"]

    def test_goods_beyond_capacity_land_in_the_last_parcel_of_that_material(self):
        # The operator repacked 12 half-packs into a single "1-pak" (capacity 2).
        draft = _draft(
            amount="600.00",
            breakdown=[{"type": "1-pak", "qty": 1}],
            items=[{"name": "HUMIO 72 butelki", "quantity": 1, "line_total": "600.00"}],
        )
        assert _amounts(draft) == ["600.00"]


class TestDepositAndDiscounts:
    def test_deposit_is_spread_proportionally_without_being_a_line(self):
        # "kaucja" is filtered out of the parcel plan by SKIP_RE but the
        # customer still pays it, so it rides along inside total_outstanding.
        draft = _draft(
            amount="330.00",  # 300 goods + 30 deposit
            breakdown=[{"type": "3-pak", "qty": 1}, {"type": "pół-pak", "qty": 1}],
            items=[
                {"name": "HUMIO 42 butelki", "quantity": 1, "line_total": "300.00"},
                {"name": "Kaucja za butelki", "quantity": 30, "line_total": "30.00"},
            ],
        )
        amounts = _amounts(draft)
        assert sum(Decimal(value) for value in amounts) == Decimal("330.00")
        assert amounts == ["282.86", "47.14"]


class TestRepackRecomputes:
    def test_changing_the_breakdown_changes_the_split(self):
        items = [{"name": "HUMIO 72 butelki", "quantity": 1, "line_total": "300.00"}]
        two = _draft(amount="300.00", breakdown=[{"type": "3-pak", "qty": 2}], items=items)
        three = _draft(
            amount="300.00",
            breakdown=[{"type": "2-pak", "qty": 3}],
            items=items,
        )
        assert _amounts(two) == ["150.00", "150.00"]
        assert _amounts(three) == ["100.00", "100.00", "100.00"]

    def test_the_split_is_stable_for_the_same_draft(self):
        # A resume after a partial failure recomputes rather than reads a
        # stored value, so the second call must agree with the first.
        draft = _draft(
            amount="100.01",
            breakdown=[{"type": "1-pak", "qty": 3}],
            items=[{"name": "HUMIO 36 butelek", "quantity": 1, "line_total": "100.01"}],
        )
        assert _amounts(draft) == _amounts(draft)


class TestLegacyDraftsWithoutPrices:
    def test_missing_line_totals_fall_back_to_an_equal_split(self):
        draft = _draft(
            amount="300.00",
            breakdown=[{"type": "3-pak", "qty": 1}, {"type": "pół-pak", "qty": 1}],
            items=[{"name": "HUMIO 42 butelki", "quantity": 1}],
        )
        allocation = cod_allocation(draft)
        assert [str(value) for value in allocation.amounts] == ["150.00", "150.00"]
        assert allocation.basis == "equal"

    def test_a_priced_draft_reports_a_value_basis(self):
        draft = _draft(
            amount="300.00",
            breakdown=[{"type": "3-pak", "qty": 1}],
            items=[{"name": "HUMIO 36 butelek", "quantity": 1, "line_total": "300.00"}],
        )
        assert cod_allocation(draft).basis == "value"

    def test_a_missing_shipping_price_is_treated_as_zero(self):
        draft = _draft(
            amount="300.00",
            shipping_price=None,
            breakdown=[{"type": "3-pak", "qty": 2}],
            items=[{"name": "HUMIO 72 butelki", "quantity": 1, "line_total": "300.00"}],
        )
        assert _amounts(draft) == ["150.00", "150.00"]


class TestFailClosed:
    def test_shipping_above_the_collected_total_is_refused(self):
        # A partial payment can leave less outstanding than the shipping line.
        # Splitting a negative goods pot would invent money.
        draft = _draft(
            amount="10.00",
            shipping_price="30.00",
            breakdown=[{"type": "3-pak", "qty": 2}],
            items=[{"name": "HUMIO 72 butelki", "quantity": 1, "line_total": "300.00"}],
        )
        with pytest.raises(CodAllocationError, match=r"(?i)shipping price"):
            cod_allocation(draft)

    def test_a_parcel_worth_nothing_is_refused(self):
        # Free shipping plus an empty second box would ask the courier to
        # collect 0.00, which both carriers reject at the API boundary.
        draft = _draft(
            amount="300.00",
            breakdown=[{"type": "3-pak", "qty": 2}],
            items=[{"name": "HUMIO 36 butelek", "quantity": 1, "line_total": "300.00"}],
        )
        with pytest.raises(CodAllocationError, match=r"0\.00"):
            cod_allocation(draft)

    def test_a_draft_without_cod_is_refused(self):
        draft = {"packages_breakdown": [{"type": "1-pak", "qty": 1}], "order_items": []}
        with pytest.raises(CodAllocationError, match=r"no COD"):
            cod_allocation(draft)


class TestParcelCapacityTable:
    """The half_packs table is what the value split divides by, so it must
    agree with the planner that produced the boxes in the first place."""

    def test_every_parcel_type_declares_its_capacity(self):
        assert set(PARCEL_HALF_PACKS) == set(PARCEL_SPECS)
        assert all(capacity > 0 for capacity in PARCEL_HALF_PACKS.values())

    @given(
        plastic=st.integers(min_value=0, max_value=40),
        glass=st.integers(min_value=0, max_value=40),
    )
    def test_planned_boxes_always_hold_what_was_ordered(self, plastic: int, glass: int):
        items = []
        if plastic:
            items.append({"name": f"HUMIO {plastic * 6} butelek", "quantity": 1})
        if glass:
            items.append({"name": f"HUMIO w szkle {glass * 6} butelek", "quantity": 1})
        plan = calc_packages(items)
        capacity = sum(PARCEL_HALF_PACKS[box.package_type] * box.quantity for box in plan.breakdown)
        assert capacity >= plastic + glass
