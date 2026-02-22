"""Tests for zdrovena.audit.bottles."""

from __future__ import annotations

import pytest

from zdrovena.audit.bottles import (
    BOTTLE_PRODUCTS,
    bottles_per_unit,
    extract_bottles,
    invoice_bottles,
    is_glass,
    wz_bottles,
)


# ── bottles_per_unit ──────────────────────────────────────────────────────────

class TestBottlesPerUnit:
    def test_butelek_pattern(self):
        assert bottles_per_unit("Woda Humio 500ml - 12 butelek") == 12
        assert bottles_per_unit("Woda 36 butelek") == 36

    def test_x_pattern(self):
        assert bottles_per_unit("Humio 500ml x 12") == 12
        assert bottles_per_unit("Woda x6") == 6

    def test_zgrzewka_fixed_count(self):
        assert bottles_per_unit("Zgrzewka wody Humio") == 12

    def test_zestaw_testowy(self):
        assert bottles_per_unit("Zestaw testowy Humio") == 6

    def test_no_match(self):
        assert bottles_per_unit("Dostawa kurierska") == 0
        assert bottles_per_unit("Opakowanie") == 0

    def test_case_insensitive(self):
        assert bottles_per_unit("ZGRZEWKA WODY") == 12
        assert bottles_per_unit("20 BUTELEK") == 20


# ── is_glass ──────────────────────────────────────────────────────────────────

class TestIsGlass:
    def test_szklo_detected(self):
        assert is_glass("Woda Humio szkło 500ml") is True

    def test_szkle_detected(self):
        assert is_glass("butelka w szkle") is True

    def test_not_glass(self):
        assert is_glass("Woda Humio 500ml") is False
        assert is_glass("plastik") is False


# ── extract_bottles ───────────────────────────────────────────────────────────

class TestExtractBottles:
    def test_plastic(self):
        assert extract_bottles("Woda Humio 500ml x 12", 3) == (36, 0)

    def test_glass(self):
        assert extract_bottles("Woda szkło 6 butelek", 2) == (0, 12)

    def test_shipping_skipped(self):
        assert extract_bottles("Dostawa InPost", 1) == (0, 0)
        assert extract_bottles("Kurier DPD", 1) == (0, 0)
        assert extract_bottles("Paczkomat A 123", 1) == (0, 0)

    def test_non_bottle_product(self):
        assert extract_bottles("Koszulka firmowa", 5) == (0, 0)

    def test_allegro_skipped(self):
        assert extract_bottles("Allegro prowizja", 1) == (0, 0)


# ── invoice_bottles ───────────────────────────────────────────────────────────

class TestInvoiceBottles:
    def test_sums_positions(self, sample_invoice):
        plastic, glass = invoice_bottles(sample_invoice)
        # 3 × 12 butelek = 36 plastic + 1 × 6 butelek glass = 6
        assert plastic == 36
        assert glass == 6

    def test_receipt(self, sample_receipt):
        plastic, glass = invoice_bottles(sample_receipt)
        # 2 × 12 butelek = 24 plastic
        assert plastic == 24
        assert glass == 0

    def test_empty_invoice(self):
        inv = {"id": 99, "positions": []}
        assert invoice_bottles(inv) == (0, 0)

    def test_no_positions_key(self):
        inv = {"id": 99}
        assert invoice_bottles(inv) == (0, 0)


# ── wz_bottles ────────────────────────────────────────────────────────────────

class TestWzBottles:
    def test_counts_bottles(self, sample_wz_actions):
        p, g = wz_bottles(201, sample_wz_actions)
        assert p == 36
        assert g == 6

    def test_plastic_only(self, sample_wz_actions):
        p, g = wz_bottles(202, sample_wz_actions)
        assert p == 12
        assert g == 0

    def test_missing_doc_returns_zeros(self, sample_wz_actions):
        assert wz_bottles(999, sample_wz_actions) == (0, 0)


# ── BOTTLE_PRODUCTS ───────────────────────────────────────────────────────────

class TestBottleProducts:
    def test_is_frozenset(self):
        assert isinstance(BOTTLE_PRODUCTS, frozenset)

    def test_contains_expected(self):
        assert "Woda Humio butelka" in BOTTLE_PRODUCTS
        assert "Woda Humio butelka plastik" in BOTTLE_PRODUCTS
        assert "Woda Humio butelka szkło" in BOTTLE_PRODUCTS
