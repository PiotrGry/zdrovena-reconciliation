"""Tests for zdrovena.audit.bottles."""

from __future__ import annotations

from typing import ClassVar

import pytest

from zdrovena.audit.bottles import (
    BOTTLE_ALIASES,
    BOTTLE_PRODUCTS,
    bottles_per_unit,
    extract_bottles,
    invoice_bottle_details,
    invoice_bottles,
    is_glass,
    wz_bottles,
)

# ── bottles_per_unit ──────────────────────────────────────────────────────────


class TestBottlesPerUnit:
    def test_butelek_pattern(self):
        assert bottles_per_unit("Woda Humio 500ml - 12 butelek") == 12
        assert bottles_per_unit("Woda 36 butelek") == 36
        assert bottles_per_unit("Woda 24 butelki") == 24
        assert bottles_per_unit("Woda 1 butelka") == 1

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

    def test_kaucja_skipped(self):
        assert extract_bottles("Kaucja za butelki 12 butelek", 5) == (0, 0)
        assert extract_bottles("kaucja szklana", 1) == (0, 0)


# ── invoice_bottle_details ────────────────────────────────────────────────────


class TestInvoiceBottleDetails:
    def test_returns_details(self):
        inv = {
            "positions": [
                {"name": "Woda Humio 500ml x 12", "quantity": "2"},
            ]
        }
        total, details = invoice_bottle_details(inv)
        assert total == 24
        assert len(details) == 1
        name, qty, bpu, cnt = details[0]
        assert name == "Woda Humio 500ml x 12"
        assert qty == 2
        assert bpu == 12
        assert cnt == 24

    def test_skips_skip_pattern(self):
        inv = {
            "positions": [
                {"name": "Dostawa kurierska", "quantity": "1"},
            ]
        }
        total, _details = invoice_bottle_details(inv)
        assert total == 0
        assert _details == []

    def test_skips_positions_without_bottles(self):
        inv = {
            "positions": [
                {"name": "Koszulka firmowa", "quantity": "5"},
            ]
        }
        total, _details = invoice_bottle_details(inv)
        assert total == 0

    def test_multiple_positions(self):
        inv = {
            "positions": [
                {"name": "Zgrzewka wody Humio", "quantity": "3"},  # 3×12=36 plastic
                {"name": "Woda szkło 6 butelek", "quantity": "2"},  # 2×6=12 glass
                {"name": "Dostawa DPD", "quantity": "1"},  # skip
            ]
        }
        total, details = invoice_bottle_details(inv)
        # invoice_bottle_details counts all bpu regardless of glass/plastic
        assert total == 36 + 12
        assert len(details) == 2

    def test_empty_positions(self):
        total, details = invoice_bottle_details({"positions": []})
        assert total == 0
        assert details == []


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
        assert "Woda Humio butelka plastik" in BOTTLE_PRODUCTS
        assert "Woda Humio butelka szkło" in BOTTLE_PRODUCTS

    def test_legacy_name_not_in_products(self):
        assert "Woda Humio butelka" not in BOTTLE_PRODUCTS


class TestBottleAliases:
    def test_legacy_alias(self):
        assert BOTTLE_ALIASES["Woda Humio butelka"] == "Woda Humio butelka plastik"

    def test_wz_bottles_resolves_legacy(self):
        """Old WZ actions with legacy product name should count as plastik."""
        actions = {
            300: [
                {
                    "warehouse_document_id": 300,
                    "product_name": "Woda Humio butelka",
                    "quantity": "-24",
                },
            ],
        }
        p, g = wz_bottles(300, actions)
        assert p == 24
        assert g == 0


# ── Real product catalogue ───────────────────────────────────────────────────


class TestProductionCatalogue:
    """Every product name that has reached a shipping draft in production.

    The shop renames products without warning: on 2026-08-17 the glass SKU
    became "w szklanych butelkach - 12 szt.", which matched neither the glass
    pattern (adjectival "szklanych") nor any bottle-count pattern ("szt."
    instead of "butelek"). Orders #1710, #1711 and #1712 were planned as
    plastic boxes. This matrix is the regression guard: a rename that this
    module cannot read must fail here, not in the warehouse.
    """

    # (name, bottles per unit, is glass)
    CATALOGUE: ClassVar[list[tuple[str, int, bool]]] = [
        ("HUMIO - Alkaliczna Woda Humusowa 500ml x 12", 12, False),
        ("HUMIO - Aklaliczna Woda Humusowa 500ml x 12", 12, False),
        ("HUMIO - woda alkaliczna, 12 butelek", 12, False),
        ("Woda alkaliczna HUMIO z kwasami humusowymi – 12 butelek", 12, False),
        ("Przetestuj wodę HUMIO, 6 butelek", 6, False),
        ("Miesięczny zapas wody HUMIO - 36 butelek", 36, False),
        ("Imprezowy zapas wody HUMIO – 72 butelki", 72, False),
        ("Imprezowy zapas wody HUMIO - 72 butelek", 72, False),
        ("HUMIO - woda alkaliczna, 12 butelek w szkle", 12, True),
        ("HUMIO - Aklaliczna Woda Humusowa w szkle 500ml x 12", 12, True),
        ("HUMIO - Alkaliczna Woda Humusowa w szkle 500ml x 12", 12, True),
        ("Woda alkaliczna HUMIO w szklanych butelkach – 12 szt.", 12, True),
    ]

    @pytest.mark.parametrize("name,expected_bpu,expected_glass", CATALOGUE)
    def test_catalogue_name_is_read_correctly(self, name, expected_bpu, expected_glass):
        assert bottles_per_unit(name) == expected_bpu
        assert is_glass(name) is expected_glass

    @pytest.mark.parametrize("name,expected_bpu,expected_glass", CATALOGUE)
    def test_catalogue_name_splits_plastic_and_glass(self, name, expected_bpu, expected_glass):
        plastic, glass = extract_bottles(name, 2)
        if expected_glass:
            assert (plastic, glass) == (0, expected_bpu * 2)
        else:
            assert (plastic, glass) == (expected_bpu * 2, 0)

    @pytest.mark.parametrize(
        "name",
        [
            "12 sztucznych wkładów",
            "Zestaw 2 szklanek – 2 szt.",
            "Kubek termiczny HUMIO",
            "HUMIO 6 PET",
        ],
    )
    def test_non_bottle_merchandise_stays_unreadable(self, name):
        """ "szt." only counts bottles when the name says bottles.

        Without the bottle context, "12 sztucznych wkładów" reads as 12 bottles
        and a set of drinking glasses reads as glass zgrzewki. Returning 0 keeps
        these out of the parcel plan and out of the month-close reconciliation,
        and sends the draft to an operator instead.
        """
        assert bottles_per_unit(name) == 0
        assert extract_bottles(name, 2) == (0, 0)

    @pytest.mark.parametrize(
        "name,expected",
        [
            ("Woda alkaliczna HUMIO w szklanych butelkach – 12 szt.", 12),
            ("HUMIO butelki 24 szt", 24),
            ("HUMIO butelka 6 sztuk", 6),
        ],
    )
    def test_szt_counts_when_the_name_mentions_bottles(self, name, expected):
        assert bottles_per_unit(name) == expected

    def test_kaucja_line_is_still_skipped_despite_the_wider_glass_pattern(self):
        """A kaucja line matches the wider glass pattern, so the skip rule must win."""
        assert extract_bottles("kaucja szklana", 1) == (0, 0)
        assert extract_bottles("kaucja 18zł, 36 butelek plastik", 1) == (0, 0)
