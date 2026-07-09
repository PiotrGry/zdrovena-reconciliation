"""Tests for zdrovena.common.bottles.count_pet_bottles — kaucja calculator.

count_pet_bottles is used by the Allegro invoice patcher to determine
how many PET bottles should incur kaucja (0.50 PLN each). Glass bottles
never incur kaucja.

Also covers the re-export shim from zdrovena.audit.bottles.
"""

from __future__ import annotations

from zdrovena.common.bottles import count_pet_bottles

# ── Basic PET counting ────────────────────────────────────────────────────────


class TestCountPetBottlesBasic:
    def test_single_position_pet_x_notation(self):
        positions = [{"name": "HUMIO 500ml x 12", "quantity": 2}]
        assert count_pet_bottles(positions) == 24

    def test_single_position_pet_butelek_notation(self):
        positions = [{"name": "HUMIO 12 butelek", "quantity": 3}]
        assert count_pet_bottles(positions) == 36

    def test_single_position_zgrzewka_fixed_count(self):
        positions = [{"name": "zgrzewka wody humio", "quantity": 2}]
        assert count_pet_bottles(positions) == 24  # 12 * 2

    def test_zestaw_testowy_fixed_count(self):
        positions = [{"name": "zestaw testowy Humio", "quantity": 1}]
        assert count_pet_bottles(positions) == 6

    def test_quantity_one_default(self):
        positions = [{"name": "HUMIO 500ml x 12", "quantity": 1}]
        assert count_pet_bottles(positions) == 12


# ── Glass exclusion ───────────────────────────────────────────────────────────


class TestGlassExclusion:
    def test_szkle_position_excluded(self):
        """'500ml w szkle x 12' must not count towards PET kaucja."""
        positions = [{"name": "HUMIO 500ml w szkle x 12", "quantity": 2}]
        assert count_pet_bottles(positions) == 0

    def test_szklo_position_excluded(self):
        """'szkło' also indicates glass."""
        positions = [{"name": "Woda alkaliczna szkło 12", "quantity": 1}]
        assert count_pet_bottles(positions) == 0

    def test_mixed_pet_and_glass(self):
        """PET + glass in one invoice: only PET counted."""
        positions = [
            {"name": "HUMIO 500ml x 12", "quantity": 2},  # 24 PET
            {"name": "HUMIO 500ml w szkle x 12", "quantity": 3},  # 36 glass → 0
        ]
        assert count_pet_bottles(positions) == 24


# ── Skip filters ──────────────────────────────────────────────────────────────


class TestSkipFilters:
    def test_kurier_skipped(self):
        positions = [{"name": "Kurier - dostawa pod drzwi", "quantity": 1}]
        assert count_pet_bottles(positions) == 0

    def test_kaucja_skipped(self):
        """Existing kaucja line must not double-count."""
        positions = [{"name": "Kaucja za butelki", "quantity": 24}]
        assert count_pet_bottles(positions) == 0

    def test_pobraniem_skipped(self):
        positions = [{"name": "Opłata za pobraniem", "quantity": 1}]
        assert count_pet_bottles(positions) == 0

    def test_inpost_shipping_skipped(self):
        positions = [{"name": "InPost Paczkomat 24/7", "quantity": 1}]
        assert count_pet_bottles(positions) == 0

    def test_koszt_dostawy_skipped(self):
        positions = [{"name": "Koszt dostawy", "quantity": 1}]
        assert count_pet_bottles(positions) == 0

    def test_allegro_orlen_skipped(self):
        """Real fixture from Allegro invoice: 'Allegro Automat ORLEN Paczka'."""
        positions = [{"name": "Allegro Automat ORLEN Paczka", "quantity": 1}]
        assert count_pet_bottles(positions) == 0


# ── Real fixtures from screenshots ────────────────────────────────────────────


class TestRealFixtures:
    def test_shopify_invoice_with_kurier(self):
        """Real Shopify invoice: 1x kurier + 2x HUMIO x12 PET = 24 bottles."""
        positions = [
            {"name": "Kurier - dostawa pod drzwi", "quantity": 1},
            {"name": "HUMIO - woda alkaliczna, 12 butelek", "quantity": 2},
        ]
        assert count_pet_bottles(positions) == 24

    def test_allegro_invoice_without_kaucja(self):
        """Real Allegro invoice needing kaucja: 2x HUMIO 500ml x12 + ORLEN."""
        positions = [
            {"name": "HUMIO - Alkaliczna Woda Humusowa 500ml x 12", "quantity": 2},
            {"name": "Allegro Automat ORLEN Paczka", "quantity": 1},
        ]
        assert count_pet_bottles(positions) == 24

    def test_glass_only_invoice(self):
        """Glass-only invoice should not add kaucja."""
        positions = [
            {"name": "HUMIO - Alkaliczna Woda Humusowa w szkle 500ml x 12", "quantity": 2},
            {"name": "Kurier - dostawa pod drzwi", "quantity": 1},
        ]
        assert count_pet_bottles(positions) == 0


# ── Edge cases ────────────────────────────────────────────────────────────────


class TestEdgeCases:
    def test_empty_list(self):
        assert count_pet_bottles([]) == 0

    def test_position_without_bottles_in_name(self):
        positions = [{"name": "Suplement diety", "quantity": 5}]
        assert count_pet_bottles(positions) == 0

    def test_position_missing_name(self):
        positions = [{"quantity": 12}]
        assert count_pet_bottles(positions) == 0

    def test_position_missing_quantity(self):
        positions = [{"name": "HUMIO x 12"}]
        # Missing quantity → treated as 0 → skip
        assert count_pet_bottles(positions) == 0

    def test_negative_quantity_skipped(self):
        """Negative qty (refund/correction) must not count."""
        positions = [{"name": "HUMIO x 12", "quantity": -2}]
        assert count_pet_bottles(positions) == 0

    def test_zero_quantity_skipped(self):
        positions = [{"name": "HUMIO x 12", "quantity": 0}]
        assert count_pet_bottles(positions) == 0

    def test_string_quantity_coerced(self):
        """Fakturownia sometimes returns quantity as string."""
        positions = [{"name": "HUMIO x 12", "quantity": "3"}]
        assert count_pet_bottles(positions) == 36

    def test_invalid_string_quantity_skipped(self):
        positions = [{"name": "HUMIO x 12", "quantity": "not-a-number"}]
        assert count_pet_bottles(positions) == 0

    def test_uses_title_when_name_absent(self):
        """Allegro lineItems use 'offer.name' — pass title as fallback."""
        positions = [{"title": "HUMIO 500ml x 12", "quantity": 1}]
        assert count_pet_bottles(positions) == 12


# ── Re-export compatibility ───────────────────────────────────────────────────


class TestAuditReexport:
    """audit.bottles must still expose everything for backward compat."""

    def test_audit_bottles_exports_all_symbols(self):
        import zdrovena.audit.bottles as audit_bottles

        # All original symbols
        assert hasattr(audit_bottles, "SKIP_RE")
        assert hasattr(audit_bottles, "BUTELEK_RE")
        assert hasattr(audit_bottles, "X_RE")
        assert hasattr(audit_bottles, "GLASS_RE")
        assert hasattr(audit_bottles, "FIXED_COUNTS")
        assert hasattr(audit_bottles, "BOTTLE_PRODUCTS")
        assert hasattr(audit_bottles, "BOTTLE_ALIASES")
        assert hasattr(audit_bottles, "bottles_per_unit")
        assert hasattr(audit_bottles, "is_glass")
        assert hasattr(audit_bottles, "extract_bottles")
        assert hasattr(audit_bottles, "invoice_bottles")
        assert hasattr(audit_bottles, "invoice_bottle_details")
        assert hasattr(audit_bottles, "wz_bottles")
        # New symbol
        assert hasattr(audit_bottles, "count_pet_bottles")

    def test_audit_and_common_bottles_share_impl(self):
        """Objects must be the same — one truth source."""
        from zdrovena.audit import bottles as audit_bottles
        from zdrovena.common import bottles as common_bottles

        assert audit_bottles.SKIP_RE is common_bottles.SKIP_RE
        assert audit_bottles.bottles_per_unit is common_bottles.bottles_per_unit
        assert audit_bottles.count_pet_bottles is common_bottles.count_pet_bottles
