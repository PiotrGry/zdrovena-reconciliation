"""
ANSI terminal formatting utilities & Polish locale helpers.
"""

from __future__ import annotations

# ── ANSI escape codes ─────────────────────────────────────────────────────────
RESET   = "\033[0m"
BOLD    = "\033[1m"
DIM     = "\033[2m"
CYAN    = "\033[36m"
GREEN   = "\033[32m"
YELLOW  = "\033[33m"
RED     = "\033[31m"
MAGENTA = "\033[35m"

# ── Polish month abbreviations ───────────────────────────────────────────────
MONTHS_PL = {
    1: "STY", 2: "LUT", 3: "MAR", 4: "KWI", 5: "MAJ", 6: "CZE",
    7: "LIP", 8: "SIE", 9: "WRZ", 10: "PAŹ", 11: "LIS", 12: "GRU",
}

MONTHS_FULL = {
    1: "styczen", 2: "luty", 3: "marzec", 4: "kwiecien",
    5: "maj", 6: "czerwiec", 7: "lipiec", 8: "sierpien",
    9: "wrzesien", 10: "pazdziernik", 11: "listopad", 12: "grudzien",
}

ENGLISH_MONTHS = {
    1: "January", 2: "February", 3: "March", 4: "April",
    5: "May", 6: "June", 7: "July", 8: "August",
    9: "September", 10: "October", 11: "November", 12: "December",
}

SEP  = "=" * 110
SEP2 = "-" * 110


def status_icon(delta: int) -> str:
    """Return a status icon for a delta value."""
    if delta == 0:
        return "✅"
    return "⚠️" if abs(delta) <= 12 else "❌"


# ── Decimal helper ───────────────────────────────────────────────────────────

from decimal import Decimal, ROUND_HALF_UP


def to_decimal(value: object) -> Decimal:
    """Safely convert any value to a 2-place Decimal."""
    if value is None:
        return Decimal("0.00")
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except Exception:
        return Decimal("0.00")
