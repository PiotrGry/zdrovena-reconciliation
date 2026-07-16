"""zdrovena.common.bottles — bottle extraction logic (plastic vs glass).

Extracts bottle counts from product/position names using regex heuristics.
Used by:
  - audit/bottles.py (re-exports for month closing reconciliation)
  - workers/allegro_invoices_poller.py (kaucja calculation)
  - api/routers/webhooks.py (SKIP_RE / is_glass for package breakdown)

Rules:
  - "12 butelek" / "36 butelek" → literal count
  - "500ml x 12" / "x 6" → count after 'x'
  - "zgrzewka" → 12, "zestaw testowy" → 6 (fixed keywords)
  - Glass: name matches "szkł" or "szkle" → glass, else PET
  - Skip: shipping/fees/coupons/kaucja lines
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

# ── Patterns ──────────────────────────────────────────────────────────────────

# Non-bottle items (shipping, fees, etc.) – skip these positions entirely
SKIP_RE = re.compile(
    r"inpost|dpd|allegro|poczta|kurier|orlen|pobraniem|koszt dostawy|"
    r"niestandardowa|paczkomat|paczkopunkt|pickup|kaucja",
    re.IGNORECASE,
)

# "- 36 butelek", ", 24 butelki", "1 butelka"
BUTELEK_RE = re.compile(r"(\d+)\s*butel(?:ek|ki|ka)", re.IGNORECASE)

# "500ml x 12" (older product naming scheme)
X_RE = re.compile(r"x\s*(\d+)", re.IGNORECASE)

# Glass indicator: "szkle" or "szkło"
GLASS_RE = re.compile(r"szk[lł][eo]", re.IGNORECASE)

# Fixed-count items without explicit numeric count in the name
FIXED_COUNTS: dict[str, int] = {
    "zgrzewka": 12,
    "zestaw testowy": 6,
}

# WZ warehouse action product names that represent bottles
BOTTLE_PRODUCTS = frozenset(
    {
        "Woda Humio butelka plastik",
        "Woda Humio butelka szkło",
    }
)

# Legacy product names → current canonical name.
# Old WZ documents used "Woda Humio butelka" before the plastik/szkło split.
BOTTLE_ALIASES: dict[str, str] = {
    "Woda Humio butelka": "Woda Humio butelka plastik",
}


# ── Core extraction ──────────────────────────────────────────────────────────


def bottles_per_unit(name: str) -> int:
    """Return how many bottles a single unit of this product represents."""
    nl = name.lower()
    m = BUTELEK_RE.search(nl)
    if m:
        return int(m.group(1))
    m = X_RE.search(nl)
    if m:
        return int(m.group(1))
    for keyword, count in FIXED_COUNTS.items():
        if keyword in nl:
            return count
    return 0


def is_glass(name: str) -> bool:
    """Return True if the product name indicates glass bottles."""
    return bool(GLASS_RE.search(name))


def extract_bottles(position_name: str, qty: float) -> tuple[int, int]:
    """
    Return ``(plastic, glass)`` bottle count for one invoice position.

    Parameters
    ----------
    position_name : product / position name string from the invoice
    qty           : quantity ordered (how many units of this position)

    Returns
    -------
    tuple of (plastic_bottles, glass_bottles)
    """
    nl = position_name.lower()

    if SKIP_RE.search(nl):
        return 0, 0

    bpu = bottles_per_unit(position_name)
    if bpu == 0:
        return 0, 0

    total = int(bpu * qty)

    if is_glass(position_name):
        return 0, total
    return total, 0


def invoice_bottles(inv: dict) -> tuple[int, int]:
    """Sum ``(plastic, glass)`` across all positions of an invoice."""
    tp, tg = 0, 0
    for pos in inv.get("positions", []):
        p, g = extract_bottles(pos.get("name", ""), float(pos.get("quantity", 0)))
        tp += p
        tg += g
    return tp, tg


def invoice_bottle_details(inv: dict) -> tuple[int, list[tuple[str, int, int, int]]]:
    """
    Return ``(total_bottles, details)`` where details is a list of
    ``(name, qty, bottles_per_unit, count)`` for each bottle position.
    """
    total = 0
    details: list[tuple[str, int, int, int]] = []
    for p in inv.get("positions", []):
        name = p.get("name", "")
        qty = float(p.get("quantity", 0))
        nl = name.lower()
        if SKIP_RE.search(nl):
            continue
        bpu = bottles_per_unit(name)
        if bpu:
            cnt = int(bpu * qty)
            total += cnt
            details.append((name, int(qty), bpu, cnt))
    return total, details


def wz_bottles(wz_id: int, actions_by_doc: dict[int, list[dict]]) -> tuple[int, int]:
    """
    Count ``(plastic, glass)`` bottles from WZ warehouse actions.

    Parameters
    ----------
    wz_id          : warehouse document ID
    actions_by_doc : mapping of document_id → list of warehouse action dicts
    """
    p, g = 0, 0
    for a in actions_by_doc.get(wz_id, []):
        pname = BOTTLE_ALIASES.get(a.get("product_name", ""), a.get("product_name", ""))
        if pname in BOTTLE_PRODUCTS:
            q = int(abs(float(a["quantity"])))
            if "szkło" in pname.lower():
                g += q
            else:
                p += q
    return p, g


# ── PET-only accessors (for Allegro invoice patcher / kaucja) ─────────────────


def count_pet_bottles(positions: Iterable[dict[str, Any]]) -> int:
    """
    Count PET (plastic) bottles across all positions in an invoice/order.

    Positions may come from:
      - Fakturownia invoice (positions[].name, positions[].quantity)
      - Allegro order (lineItems[].offer.name, lineItems[].quantity)
      - Any list of dicts with 'name' and 'quantity' keys

    Skipped:
      - Non-bottle items (shipping, kaucja, coupon codes)
      - Glass positions (matches GLASS_RE — no kaucja on glass)
      - Zero/negative quantity

    Returns
    -------
    Total count of PET bottles (int, ≥0)
    """
    total = 0
    for pos in positions:
        name = pos.get("name", "") or pos.get("title", "")
        try:
            qty = float(pos.get("quantity", 0))
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            continue
        plastic, _glass = extract_bottles(name, qty)
        total += plastic
    return total
