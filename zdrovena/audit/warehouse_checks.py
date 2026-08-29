"""Warehouse consistency checks as data, not as printed output.

The audit sections in ``sections.py`` are written as a report for a human: they
compute, print, and register a failure on a Verdict. The month-close workflow
needs the same conclusions as structured issues, so the computation lives here
and the printing sections call it (issue #308).

Only the two checks with an actual failure condition are here. The stock-balance
section prints movements and current stock but never fails -- there is no rule in
it to map, and inventing a threshold is a business decision, not a refactor.
"""

from __future__ import annotations

from typing import Any

from zdrovena.audit.bottles import invoice_bottles, wz_bottles


def find_orphan_wz(
    wz_docs: list[dict[str, Any]],
    inv_by_wz: dict[int, dict[str, Any]],
    doc_actions: dict[int, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """WZ documents with no invoice linked to them.

    Goods left the warehouse and nothing was billed for them, which is why this
    reads as potentially uninvoiced sales rather than a paperwork detail.
    """
    orphans: list[dict[str, Any]] = []
    for wz in wz_docs:
        if wz["id"] in inv_by_wz:
            continue
        plastic, glass = wz_bottles(wz["id"], doc_actions)
        orphans.append({"wz": wz, "plastic": plastic, "glass": glass, "total": plastic + glass})
    return orphans


def find_invoices_without_wz(
    invoices: list[dict[str, Any]],
    inv_by_wz: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Invoices carrying bottle positions with no WZ behind them.

    Invoices without bottle positions are ignored on purpose: a service line or
    a deposit-only correction has nothing to ship and needs no warehouse document.
    """
    wz_linked_ids = {inv["id"] for inv in inv_by_wz.values()}
    missing: list[dict[str, Any]] = []
    for inv in invoices:
        if inv["id"] in wz_linked_ids:
            continue
        plastic, glass = invoice_bottles(inv)
        if plastic + glass > 0:
            missing.append(
                {"inv": inv, "plastic": plastic, "glass": glass, "total": plastic + glass}
            )
    return missing
