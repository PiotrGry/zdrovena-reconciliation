"""
Bottle extraction logic – shared across all audit commands.

DEPRECATED: This module now re-exports from ``zdrovena.common.bottles``.
Prefer importing directly from ``zdrovena.common.bottles`` in new code.
Kept for backward compatibility with existing imports (audit CLI, webhooks).
"""

from __future__ import annotations

from zdrovena.common.bottles import (
    BOTTLE_ALIASES,
    BOTTLE_PRODUCTS,
    BUTELEK_RE,
    FIXED_COUNTS,
    GLASS_RE,
    SKIP_RE,
    X_RE,
    bottles_per_unit,
    count_pet_bottles,
    extract_bottles,
    invoice_bottle_details,
    invoice_bottles,
    is_glass,
    wz_bottles,
)

__all__ = [
    "BOTTLE_ALIASES",
    "BOTTLE_PRODUCTS",
    "BUTELEK_RE",
    "FIXED_COUNTS",
    "GLASS_RE",
    "SKIP_RE",
    "X_RE",
    "bottles_per_unit",
    "count_pet_bottles",
    "extract_bottles",
    "invoice_bottle_details",
    "invoice_bottles",
    "is_glass",
    "wz_bottles",
]
