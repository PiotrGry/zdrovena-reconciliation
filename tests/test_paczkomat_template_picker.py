"""Tests for pick_paczkomat_template + _parcel_template auto-pick (P2-1)."""

from __future__ import annotations

from zdrovena.api.routers.webhooks import _parcel_template
from zdrovena.common.inpost import (
    PACZKOMAT_SLOTS,
    PARCEL_SPECS,
    _fits_in_slot,
    pick_paczkomat_template,
)

# ── _fits_in_slot ────────────────────────────────────────────────────────────


class TestFitsInSlot:
    def test_tiny_package_fits_small(self):
        # 5 × 20 × 30 → shortest 5 ≤ 8 (slot A height); mid 20 ≤ 38; long 30 ≤ 64
        assert _fits_in_slot(
            {"length": 30, "width": 20, "height": 5}, 1.0, PACZKOMAT_SLOTS["small"]
        )

    def test_over_weight_never_fits(self):
        assert not _fits_in_slot(
            {"length": 30, "width": 20, "height": 5}, 100.0, PACZKOMAT_SLOTS["large"]
        )

    def test_too_tall_for_small_but_fits_medium(self):
        # 15 × 30 × 40 → shortest 15 > small slot height (8) → fails small
        assert not _fits_in_slot(
            {"length": 40, "width": 30, "height": 15}, 5.0, PACZKOMAT_SLOTS["small"]
        )
        # But fits medium (height 19)
        assert _fits_in_slot(
            {"length": 40, "width": 30, "height": 15}, 5.0, PACZKOMAT_SLOTS["medium"]
        )

    def test_zero_dims_never_fit(self):
        assert not _fits_in_slot(
            {"length": 0, "width": 30, "height": 15}, 5.0, PACZKOMAT_SLOTS["large"]
        )

    def test_footprint_too_wide_fails(self):
        # length 100 > slot depth (64) in any orientation
        assert not _fits_in_slot(
            {"length": 100, "width": 30, "height": 5}, 5.0, PACZKOMAT_SLOTS["large"]
        )


# ── pick_paczkomat_template ──────────────────────────────────────────────────


class TestPickPaczkomatTemplate:
    def test_flat_package_picks_small(self):
        assert pick_paczkomat_template({"length": 30, "width": 20, "height": 5}, 1.0) == "small"

    def test_medium_package_picks_medium(self):
        assert pick_paczkomat_template({"length": 40, "width": 30, "height": 15}, 5.0) == "medium"

    def test_tall_package_picks_large(self):
        assert pick_paczkomat_template({"length": 40, "width": 30, "height": 25}, 5.0) == "large"

    def test_oversized_returns_none(self):
        assert pick_paczkomat_template({"length": 100, "width": 100, "height": 100}, 5.0) is None

    def test_current_1pak_fits_large(self):
        # 30 × 20 × 20 @ 6kg — shortest 20 > medium (19), so → large
        spec = PARCEL_SPECS["1-pak"]
        assert pick_paczkomat_template(dict(spec), spec["weight_kg"]) == "large"

    def test_current_box_types_route_correctly(self):
        # Box types documented as paczkomat-shippable must fit somewhere.
        # 3-pak (40×40×20) does NOT fit InPost paczkomat (slot width 38) — the
        # auto-picker returns None so operators fall back to kurier for those.
        paczkomat_ok = {"1-pak", "2-pak", "pół-pak", "szkło", "szkło-2pak"}
        for name, spec in PARCEL_SPECS.items():
            slot = pick_paczkomat_template(dict(spec), spec["weight_kg"])
            if name in paczkomat_ok:
                assert slot in {"small", "medium", "large"}, f"{name} → {slot}"
            else:
                # 3-pak et al. — too wide for any paczkomat slot
                assert slot is None, f"{name} unexpectedly fits: {slot}"


# ── _parcel_template integration ─────────────────────────────────────────────


class TestParcelTemplateIntegration:
    def test_empty_breakdown_defaults_to_large(self):
        assert _parcel_template({"packages_breakdown": []}) == "large"

    def test_pol_pak_auto_picks_medium(self):
        # pół-pak: 20 × 15 × 20 @ 3kg — shortest 15 > small (8) → medium (19)
        assert _parcel_template({"packages_breakdown": [{"type": "pół-pak", "qty": 1}]}) == "medium"

    def test_3pak_falls_back_to_static_large(self):
        # 3-pak does not fit any paczkomat slot; auto returns None so
        # _parcel_template falls back to the static 'large' spec.
        assert _parcel_template({"packages_breakdown": [{"type": "3-pak", "qty": 1}]}) == "large"

    def test_mixed_breakdown_uses_largest_box(self):
        draft = {
            "packages_breakdown": [
                {"type": "pół-pak", "qty": 1},
                {"type": "3-pak", "qty": 1},
            ]
        }
        # 3-pak dims dominate → large
        assert _parcel_template(draft) == "large"

    def test_unknown_box_type_falls_back_to_large(self):
        # unknown type → no dims → auto returns None → static loop misses → 'large'
        assert _parcel_template({"packages_breakdown": [{"type": "unknown", "qty": 1}]}) == "large"
