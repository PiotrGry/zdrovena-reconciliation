"""Canonical parcel specifications shared by shipping planning and providers."""

from __future__ import annotations

# Physical dimensions and weights per package type produced by package planning.
# Dimensions in cm; weight_kg is gross weight of a single box.
# szkło-2pak is SUSPENDED (2026-09), not deleted — see GLASS_2PAK_SUSPENDED in
# zdrovena/shipping/domain/planning.py for the reason and for the checklist to
# switch it back on. This record is a single box's dimensions and weight, which
# is why it must be re-measured before the type ships again: it always meant
# two szkło boxes, and the "sent as qty=2" this comment used to promise was
# never implemented — one label and one box's 9 kg went to the courier for two
# boxes (order #1735). While suspended, physical_parcels() expands a stored row
# into two "szkło" parcels, so this record is never sent as-is.
# paczkomat_template: InPost locker template (A=small/B=medium/C=large); None = too big for any locker.
# dpd_template / orlen_template: to be filled when those carriers are integrated.
PARCEL_SPECS: dict[str, dict] = {
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
    # SUSPENDED — stored drafts only, expanded into two "szkło" parcels.
    "szkło-2pak": {
        "length": 30,
        "width": 30,
        "height": 20,
        "weight_kg": 9.0,
        "paczkomat_template": "large",
    },
}

# How many six-bottle half-packs (zgrzewki) each box holds.
#
# Kept out of PARCEL_SPECS on purpose: that record is handed to the carriers
# verbatim as the parcel's `dimensions`, so a capacity key would travel as a
# bogus dimension. This is the capacity calc_packages fills, and the divisor the
# COD value split divides by — see zdrovena/shipping/domain/cod.py.
PARCEL_HALF_PACKS: dict[str, int] = {
    "3-pak": 6,
    "2-pak": 4,
    "1-pak": 2,
    "pół-pak": 1,
    "szkło": 2,
    # SUSPENDED — two szkło boxes' worth, kept for stored drafts.
    "szkło-2pak": 4,
}

# Max package dimensions that fit in the "large" slot of each carrier's locker/automat.
# Dimensions: height × width × depth (cm), max_weight_kg.
# ✅ = verified against carrier/aggregator website; ❓ = unverified, use with caution.
#
# P2-2: DPD dimensions verified 2026-07 against dpd.com FAQ, apaczka.pl and
# polkurier.pl (all agreed on 50×44×59 for the automat and 64×41×38 for the
# Żabka punkt). Sources cited on each entry.
LOCKER_LARGE_SLOT: dict[str, dict] = {
    "inpost": {
        "height": 41,
        "width": 38,
        "depth": 64,
        "max_weight_kg": 25,
        "verified": True,
    },  # ✅ apaczka.pl / inpost.pl
    "orlen": {
        "height": 41,
        "width": 38,
        "depth": 60,
        "max_weight_kg": 20,
        "verified": True,
    },  # ✅ apaczka.pl (60×41×38)
    "dpd_automat": {
        "height": 50,
        "width": 44,
        "depth": 59,
        "max_weight_kg": 20,
        "verified": True,
    },  # ✅ dpd.com/pl/pl/faq (2025-06); apaczka.pl DPD Pickup Station guide (2026)
    "dpd_punkt": {
        "height": 64,
        "width": 41,
        "depth": 38,
        "max_weight_kg": 20,
        "verified": True,
    },  # ✅ apaczka.pl “DPD Pickup (Drzwi-Punkt)” (sieć Żabka: 64×41×38)
}

_DEFAULT_DIMS = PARCEL_SPECS["1-pak"]
