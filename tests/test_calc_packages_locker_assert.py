"""Tests for parcel planning locker-fit warnings and their API logging boundary."""

from __future__ import annotations

import logging

from zdrovena.api.routers.webhooks import _build_draft_record
from zdrovena.shipping.domain.planning import calc_packages, package_fit_warnings

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


# ── Package fit warnings ────────────────────────────────────────────────────


class TestPackageFitWarnings:
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
        warnings = package_fit_warnings(breakdown, carrier="inpost")
        assert warnings == []

    def test_unknown_box_type_is_skipped_silently(self):
        assert package_fit_warnings([{"type": "unknown", "qty": 1}], carrier="inpost") == []

    def test_unknown_carrier_returns_empty(self):
        assert package_fit_warnings([{"type": "3-pak", "qty": 1}], carrier="does-not-exist") == []

    def test_dpd_automat_fits_all_current_boxes(self):
        # DPD large: 50×44×59, 20 kg. 3-pak weight 18 kg fits.
        warnings = package_fit_warnings([{"type": "3-pak", "qty": 1}], carrier="dpd_automat")
        assert warnings == []

    def test_oversized_box_produces_exact_warnings(self, monkeypatch):
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
        warnings = package_fit_warnings([{"type": "monster-box", "qty": 1}], carrier="inpost")

        assert warnings == [
            "box 'monster-box' (200×200×200 cm) exceeds inpost locker large slot (41×38×64 cm)",
            "box 'monster-box' weight 50.0 kg exceeds inpost locker max 25 kg",
        ]


# ── Package plan post-condition and boundary logging ─────────────────────────


class TestCalcPackagesPostCondition:
    def test_normal_plan_produces_no_warnings(self):
        plan = calc_packages([{"name": "Woda 1L", "quantity": 6}])
        count, breakdown = plan.to_legacy_tuple()

        assert count >= 1
        assert package_fit_warnings(breakdown, carrier="inpost") == []

    def test_build_draft_record_logs_existing_warning_text(self, monkeypatch, caplog):
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
        order = {
            "id": "oversized-warning",
            "order_number": 9001,
            "shipping_lines": [{"title": "InPost Kurier"}],
            "line_items": [{"name": "Woda", "quantity": 3}],
            "shipping_address": {
                "first_name": "Jan",
                "last_name": "Kowalski",
                "address1": "Testowa 1",
                "city": "Warszawa",
                "zip": "00-001",
                "phone": "500600700",
            },
            "customer": {},
            "email": "jan@example.com",
            "note_attributes": [],
        }
        caplog.set_level(logging.WARNING, logger="zdrovena.api.routers.webhooks")

        draft = _build_draft_record(order)

        assert draft["packages_breakdown"] == [{"type": "3-pak", "qty": 1}]
        assert (
            "_calc_packages: box '3-pak' (100×100×100 cm) exceeds inpost locker "
            "large slot (41×38×64 cm)"
        ) in [record.getMessage() for record in caplog.records]
