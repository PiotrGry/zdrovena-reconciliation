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
