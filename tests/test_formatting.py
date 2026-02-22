"""Tests for zdrovena.common.formatting."""

from __future__ import annotations

from decimal import Decimal

import pytest

from zdrovena.common.formatting import (
    ENGLISH_MONTHS,
    MONTHS_FULL,
    MONTHS_PL,
    SEP,
    SEP2,
    status_icon,
    to_decimal,
)


# ── Month dictionaries ───────────────────────────────────────────────────────

class TestMonthDicts:
    def test_months_pl_has_all_12(self):
        assert set(MONTHS_PL.keys()) == set(range(1, 13))

    def test_months_full_has_all_12(self):
        assert set(MONTHS_FULL.keys()) == set(range(1, 13))

    def test_english_months_has_all_12(self):
        assert set(ENGLISH_MONTHS.keys()) == set(range(1, 13))

    def test_english_months_values(self):
        assert ENGLISH_MONTHS[1] == "January"
        assert ENGLISH_MONTHS[12] == "December"

    def test_months_pl_abbreviations(self):
        assert MONTHS_PL[1] == "STY"
        assert MONTHS_PL[6] == "CZE"
        assert MONTHS_PL[12] == "GRU"

    def test_months_full_no_diacritics(self):
        """Full month names should be filesystem-safe (no diacritics)."""
        for name in MONTHS_FULL.values():
            assert name.isascii(), f"{name!r} contains non-ASCII characters"


# ── Separators ────────────────────────────────────────────────────────────────

class TestSeparators:
    def test_sep_length(self):
        assert len(SEP) == 110

    def test_sep2_length(self):
        assert len(SEP2) == 110

    def test_sep_characters(self):
        assert set(SEP) == {"="}
        assert set(SEP2) == {"-"}


# ── status_icon ───────────────────────────────────────────────────────────────

class TestStatusIcon:
    def test_zero_is_ok(self):
        assert status_icon(0) == "✅"

    def test_small_delta_is_warning(self):
        assert status_icon(5) == "⚠️"
        assert status_icon(-12) == "⚠️"
        assert status_icon(12) == "⚠️"

    def test_large_delta_is_error(self):
        assert status_icon(13) == "❌"
        assert status_icon(-50) == "❌"
        assert status_icon(100) == "❌"


# ── to_decimal ────────────────────────────────────────────────────────────────

class TestToDecimal:
    def test_integer(self):
        assert to_decimal(42) == Decimal("42.00")

    def test_float(self):
        result = to_decimal(3.145)
        assert result == Decimal("3.15")  # rounds half up

    def test_string(self):
        assert to_decimal("123.456") == Decimal("123.46")

    def test_none_returns_zero(self):
        assert to_decimal(None) == Decimal("0.00")

    def test_invalid_string_returns_zero(self):
        assert to_decimal("not-a-number") == Decimal("0.00")

    def test_negative(self):
        assert to_decimal(-99.999) == Decimal("-100.00")

    def test_already_decimal(self):
        d = Decimal("10.50")
        assert to_decimal(d) == Decimal("10.50")

    def test_zero(self):
        assert to_decimal(0) == Decimal("0.00")
