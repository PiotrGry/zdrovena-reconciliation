"""Tests for _assert_packages_fit_locker + _calc_packages post-condition (P2-3)."""

from __future__ import annotations

import logging

from zdrovena.api.routers.webhooks import (
    _assert_packages_fit_locker,
    _calc_packages,
)

# ── Package catalog snapshots ─────────────────────────────────────────────────────


class TestPackageCatalogSnapshot:
    def test_inpost_catalog_exports_alias_canonical_mutable_objects(self):
        from zdrovena.common import inpost, shipping_parcels

        assert inpost.PARCEL_SPECS is shipping_parcels.PARCEL_SPECS
        assert inpost.LOCKER_LARGE_SLOT is shipping_parcels.LOCKER_LARGE_SLOT
        assert inpost._DEFAULT_DIMS is shipping_parcels._DEFAULT_DIMS

    def test_current_package_types_are_exactly_preserved(self):
        from zdrovena.common.inpost import PARCEL_SPECS

        assert set(PARCEL_SPECS) == {
            "3-pak",
            "2-pak",
            "1-pak",
            "pół-pak",
            "szkło",
            "szkło-2pak",
        }

    def test_package_dimensions_weights_and_templates_are_unchanged(self):
        from zdrovena.common.inpost import PARCEL_SPECS

        assert PARCEL_SPECS == {
            "3-pak": {
                "length": 40,
                "width": 40,
                "height": 20,
                "weight_kg": 18.0,
                "paczkomat_template": "large",
            },
            "2-pak": {
                "length": 40,
                "width": 30,
                "height": 20,
                "weight_kg": 12.0,
                "paczkomat_template": "large",
            },
            "1-pak": {
                "length": 30,
                "width": 20,
                "height": 20,
                "weight_kg": 6.0,
                "paczkomat_template": "large",
            },
            "pół-pak": {
                "length": 20,
                "width": 15,
                "height": 20,
                "weight_kg": 3.0,
                "paczkomat_template": "large",
            },
            "szkło": {
                "length": 30,
                "width": 30,
                "height": 20,
                "weight_kg": 9.0,
                "paczkomat_template": "large",
            },
            "szkło-2pak": {
                "length": 30,
                "width": 30,
                "height": 20,
                "weight_kg": 9.0,
                "paczkomat_template": "large",
            },
        }

    def test_cross_carrier_locker_limits_are_unchanged(self):
        from zdrovena.common.inpost import (
            CARRIER_LOCKER_SLOTS,
            LOCKER_LARGE_SLOT,
            PACZKOMAT_SLOTS,
        )

        assert PACZKOMAT_SLOTS == {
            "small": {"height": 8, "width": 38, "depth": 64, "max_weight_kg": 25},
            "medium": {"height": 19, "width": 38, "depth": 64, "max_weight_kg": 25},
            "large": {"height": 41, "width": 38, "depth": 64, "max_weight_kg": 25},
        }

        assert LOCKER_LARGE_SLOT == {
            "inpost": {
                "height": 41,
                "width": 38,
                "depth": 64,
                "max_weight_kg": 25,
                "verified": True,
            },
            "orlen": {
                "height": 41,
                "width": 38,
                "depth": 60,
                "max_weight_kg": 20,
                "verified": True,
            },
            "dpd_automat": {
                "height": 50,
                "width": 44,
                "depth": 59,
                "max_weight_kg": 20,
                "verified": True,
            },
            "dpd_punkt": {
                "height": 64,
                "width": 41,
                "depth": 38,
                "max_weight_kg": 20,
                "verified": True,
            },
        }
        assert CARRIER_LOCKER_SLOTS == {
            "inpost": [
                {"name": "A", "height": 8, "width": 38, "depth": 64, "max_weight_kg": 25},
                {"name": "B", "height": 19, "width": 38, "depth": 64, "max_weight_kg": 25},
                {"name": "C", "height": 41, "width": 38, "depth": 64, "max_weight_kg": 25},
            ],
            "dpd_automat": [
                {"name": "S", "height": 11, "width": 44, "depth": 59, "max_weight_kg": 20},
                {"name": "M", "height": 24, "width": 44, "depth": 59, "max_weight_kg": 20},
                {"name": "L", "height": 50, "width": 44, "depth": 59, "max_weight_kg": 20},
            ],
        }


# ── _assert_packages_fit_locker ─────────────────────────────────────────────


class TestAssertPackagesFitLocker:
    def test_all_current_boxes_fit_inpost_large_slot(self):
        # Every PARCEL_SPECS entry must fit the InPost L slot (41×38×64, 25 kg).
        breakdown = [
            {"type": "3-pak", "qty": 1},
            {"type": "2-pak", "qty": 1},
            {"type": "1-pak", "qty": 1},
            {"type": "pół-pak", "qty": 1},
            {"type": "szkło", "qty": 1},
            {"type": "szkło-2pak", "qty": 1},
        ]
        warnings = _assert_packages_fit_locker(breakdown, carrier="inpost")
        assert warnings == []

    def test_unknown_box_type_is_skipped_silently(self):
        assert _assert_packages_fit_locker([{"type": "unknown", "qty": 1}], carrier="inpost") == []

    def test_unknown_carrier_returns_empty(self):
        assert (
            _assert_packages_fit_locker([{"type": "3-pak", "qty": 1}], carrier="does-not-exist")
            == []
        )

    def test_dpd_automat_fits_all_current_boxes(self):
        # DPD large: 50×44×59, 20 kg. 3-pak weight 18 kg fits.
        warnings = _assert_packages_fit_locker([{"type": "3-pak", "qty": 1}], carrier="dpd_automat")
        assert warnings == []

    def test_oversized_box_produces_warning(self, monkeypatch, caplog):
        # Inject an oversized entry into PARCEL_SPECS to prove the guard fires.
        from zdrovena.common import inpost

        monkeypatch.setitem(
            inpost.PARCEL_SPECS,
            "monster-box",
            {
                "length": 200,
                "width": 200,
                "height": 200,
                "weight_kg": 50.0,
                "paczkomat_template": "large",
            },
        )
        caplog.set_level(logging.WARNING)
        warnings = _assert_packages_fit_locker(
            [{"type": "monster-box", "qty": 1}], carrier="inpost"
        )
        assert len(warnings) >= 1
        assert any("monster-box" in w for w in warnings)
        # Also emitted to logger
        assert any("monster-box" in rec.message for rec in caplog.records)


# ── _calc_packages emits warnings for mis-configured specs ───────────────────


class TestCalcPackagesPostCondition:
    def test_normal_order_produces_no_warnings(self, caplog):
        caplog.set_level(logging.WARNING)
        count, _breakdown = _calc_packages([{"name": "Woda 1L", "quantity": 6}])
        assert count >= 1
        # No 'exceeds' warnings from the assertion helper
        assert not any("exceeds" in rec.message for rec in caplog.records)

    def test_calc_packages_warns_when_spec_oversized(self, monkeypatch, caplog):
        from zdrovena.common import inpost

        # Enlarge '3-pak' beyond the InPost slot to prove the assertion is wired in.
        monkeypatch.setitem(
            inpost.PARCEL_SPECS,
            "3-pak",
            {
                "length": 100,
                "width": 100,
                "height": 100,
                "weight_kg": 18.0,
                "paczkomat_template": "large",
            },
        )
        caplog.set_level(logging.WARNING)
        _calc_packages([{"name": "Woda", "quantity": 3}])
        assert any("3-pak" in rec.message and "exceeds" in rec.message for rec in caplog.records)
