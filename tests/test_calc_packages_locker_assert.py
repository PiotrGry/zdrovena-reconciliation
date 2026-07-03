"""Tests for _assert_packages_fit_locker + _calc_packages post-condition (P2-3)."""

from __future__ import annotations

import logging

from zdrovena.api.routers.webhooks import (
    _assert_packages_fit_locker,
    _calc_packages,
)

# ── _assert_packages_fit_locker ──────────────────────────────────────────────


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
