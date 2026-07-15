"""Tests for zdrovena.common.kaucja.calculate_kaucja — the single canonical
kaucja amount source (native Allegro per-line deposit × quantity)."""

from __future__ import annotations

from decimal import Decimal

from zdrovena.common.kaucja import calculate_kaucja


def _line(*, deposit: str | None, quantity: int = 1) -> dict:
    line: dict = {"quantity": quantity, "price": {"amount": "10.00"}}
    if deposit is not None:
        line["deposit"] = {"price": {"amount": deposit}}
    return line


def test_single_line_multiplies_deposit_by_quantity():
    order = {"lineItems": [_line(deposit="6.00", quantity=2)]}
    assert calculate_kaucja(order) == Decimal("12.00")


def test_sums_across_lines():
    order = {
        "lineItems": [
            _line(deposit="6.00", quantity=2),  # 12.00
            _line(deposit="3.00", quantity=1),  # 3.00
        ]
    }
    assert calculate_kaucja(order) == Decimal("15.00")


def test_lines_without_deposit_contribute_zero():
    order = {
        "lineItems": [
            _line(deposit=None, quantity=5),
            _line(deposit="6.00", quantity=1),
        ]
    }
    assert calculate_kaucja(order) == Decimal("6.00")


def test_no_line_items_is_zero():
    assert calculate_kaucja({}) == Decimal("0")
    assert calculate_kaucja({"lineItems": []}) == Decimal("0")


def test_missing_quantity_defaults_to_one():
    order = {"lineItems": [{"deposit": {"price": {"amount": "6.00"}}}]}
    assert calculate_kaucja(order) == Decimal("6.00")


def test_matches_real_production_order():
    """quantity=2, price 73.00, deposit 6.00, totalToPay 158.00 → kaucja 12.00."""
    order = {"lineItems": [_line(deposit="6.00", quantity=2)]}
    kaucja = calculate_kaucja(order)
    product_total = Decimal("73.00") * 2
    assert product_total + kaucja == Decimal("158.00")


# ── R4.2: explicit quantity validation (never silently 0 → 1) ───────────────

import pytest  # noqa: E402

from zdrovena.common.kaucja import parse_line_quantity  # noqa: E402


class TestParseLineQuantity:
    def test_absent_defaults_to_one(self):
        assert parse_line_quantity(None) == 1

    def test_zero_stays_zero(self):
        # The core R4.2 rule: zero units must NOT be silently promoted to one.
        assert parse_line_quantity(0) == 0

    def test_positive_preserved(self):
        assert parse_line_quantity(3) == 3
        assert parse_line_quantity("4") == 4

    def test_negative_raises(self):
        with pytest.raises(ValueError):
            parse_line_quantity(-1)

    def test_non_numeric_raises(self):
        with pytest.raises(ValueError):
            parse_line_quantity("abc")

    @pytest.mark.parametrize(
        "bad",
        [
            1.5,  # fractional float
            1.0,  # integral float — still a float, reject (no silent coercion)
            "1.5",  # fractional string
            True,  # bool must NOT become 1
            False,  # bool must NOT become 0
        ],
    )
    def test_rejects_non_integer_and_bool(self, bad):
        with pytest.raises(ValueError):
            parse_line_quantity(bad)

    @pytest.mark.parametrize("good,expected", [(0, 0), (3, 3), ("4", 4), (None, 1)])
    def test_accepts_valid(self, good, expected):
        assert parse_line_quantity(good) == expected


class TestKaucjaQuantityEdge:
    def test_zero_quantity_line_contributes_zero_not_full_deposit(self):
        order = {"lineItems": [_line(deposit="6.00", quantity=0)]}
        assert calculate_kaucja(order) == Decimal("0")

    def test_zero_quantity_does_not_inflate_multi_line_total(self):
        order = {
            "lineItems": [
                _line(deposit="6.00", quantity=0),  # 0.00 — not 6.00
                _line(deposit="3.00", quantity=2),  # 6.00
            ]
        }
        assert calculate_kaucja(order) == Decimal("6.00")


class TestDecimalSafety:
    def test_no_binary_float_drift(self):
        # 3 × 0.10 must be exactly 0.30, not 0.30000000000000004.
        order = {"lineItems": [_line(deposit="0.10", quantity=3)]}
        assert calculate_kaucja(order) == Decimal("0.30")

    def test_return_type_is_decimal(self):
        assert isinstance(calculate_kaucja({"lineItems": [_line(deposit="6.00")]}), Decimal)


class TestMapperPatcherParity:
    """Mapper and patcher must derive kaucja from the same canonical source, so
    an order can never produce two different deposit amounts."""

    def test_mapper_settlement_equals_calculate_kaucja(self):
        from zdrovena.common.allegro_invoice_mapper import allegro_order_to_fakturownia_invoice

        order = {
            "id": "af1",
            "buyer": {"email": "b@x.pl", "firstName": "A", "lastName": "B"},
            "invoice": {"required": True, "address": None},
            "lineItems": [
                {
                    "offer": {"name": "HUMIO 6 PET"},
                    "quantity": 2,
                    "price": {"amount": "73.00"},
                    "tax": {"rate": "8"},
                    "deposit": {"price": {"amount": "6.00"}},
                }
            ],
        }
        invoice = allegro_order_to_fakturownia_invoice(order)
        canonical = calculate_kaucja(order)
        settlement_amount = Decimal(invoice["settlement_positions"][0]["amount"])
        assert settlement_amount == canonical == Decimal("12.00")
