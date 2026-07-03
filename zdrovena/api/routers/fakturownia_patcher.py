"""zdrovena.api.routers.fakturownia_patcher — kaucja patcher for Allegro invoices.

Fakturownia's Allegro integration does NOT auto-compute kaucja (deposit) for PET
bottles — invoices come out without a deposit line. This worker fills that gap:

1. Enumerate Allegro-sourced drafts from shipping_store
2. For each order → fetch invoices via AllegroClient.list_order_invoices
3. For each Allegro invoice with a Fakturownia-linked number:
   a. GET invoice from Fakturownia (positions + existing settlements)
   b. Compute kaucja = 0.50 PLN × count_pet_bottles(positions)
   c. If invoice already has settlement with description "Kaucja za opakowania zwrotne"
      → skip (idempotent)
   d. Else → PUT settlement_positions with new charge row

Idempotency: uses the Fakturownia settlement description as the marker.

Environment vars:
    KAUCJA_UNIT_PRICE_PLN  default "0.50"
"""

from __future__ import annotations

import logging
import os
from decimal import Decimal
from typing import Any

from zdrovena.common.bottles import count_pet_bottles

logger = logging.getLogger(__name__)


# ── constants (env-overridable) ──────────────────────────────────────────────

KAUCJA_UNIT_PRICE_PLN = os.getenv("KAUCJA_UNIT_PRICE_PLN", "0.50").strip() or "0.50"
KAUCJA_DESCRIPTION = (
    os.getenv("KAUCJA_DESCRIPTION", "Kaucja za opakowania zwrotne").strip()
    or "Kaucja za opakowania zwrotne"
)


# ── stats ────────────────────────────────────────────────────────────────────


def _new_stats() -> dict[str, int]:
    return {
        "orders_scanned": 0,
        "invoices_scanned": 0,
        "patched": 0,
        "skipped_already_patched": 0,
        "skipped_no_pet": 0,
        "skipped_no_fakturownia_match": 0,
        "skipped_ambiguous_match": 0,
        "errors": 0,
    }


# ── helpers ──────────────────────────────────────────────────────────────────


def _allegro_orders_from_drafts(drafts: list[dict[str, Any]]) -> list[str]:
    """Extract external_order_ids for Allegro-source drafts, deduped, preserving order."""
    seen: set[str] = set()
    out: list[str] = []
    for d in drafts:
        if d.get("source") != "allegro":
            continue
        oid = str(d.get("external_order_id") or "").strip()
        if not oid or oid in seen:
            continue
        seen.add(oid)
        out.append(oid)
    return out


def _compute_kaucja_amount(pet_count: int) -> str:
    """Compute kaucja as PLN string with 2 decimals: pet_count × KAUCJA_UNIT_PRICE_PLN."""
    total = Decimal(pet_count) * Decimal(KAUCJA_UNIT_PRICE_PLN)
    return f"{total:.2f}"


# ── main entry point ─────────────────────────────────────────────────────────


def patch_allegro_invoices_once(
    *,
    allegro_client: Any,
    fakturownia_client: Any,
    shipping_store: Any,
) -> dict[str, int]:
    """Run one patcher cycle. Returns per-cycle stats.

    Never raises — all per-order and per-invoice failures are logged and
    counted in stats["errors"].
    """
    stats = _new_stats()

    try:
        drafts = shipping_store.list_drafts()
    # Resilience boundary: this worker must never crash a cycle (see docstring).
    # Any storage backend failure is counted and the cycle aborts cleanly.
    except Exception:
        logger.exception("shipping_store.list_drafts failed")
        stats["errors"] += 1
        return stats

    order_ids = _allegro_orders_from_drafts(drafts)
    stats["orders_scanned"] = len(order_ids)
    if not order_ids:
        return stats

    for order_id in order_ids:
        try:
            allegro_invoices = allegro_client.list_order_invoices(order_id)
        # Resilience boundary: a single order failure must not abort the whole cycle.
        except Exception:
            logger.exception("Allegro list_order_invoices(%s) failed", order_id)
            stats["errors"] += 1
            continue

        for allegro_inv in allegro_invoices or []:
            # Allegro's GET /invoices returns `invoiceNumber` (NOT `number`); see
            # docs/audit/fixtures/allegro_get_invoices.json.
            invoice_number = (allegro_inv.get("invoiceNumber") or "").strip()
            if not invoice_number:
                logger.warning(
                    "Allegro invoice for order %s has no `invoiceNumber` — skipping", order_id
                )
                stats["errors"] += 1
                continue

            stats["invoices_scanned"] += 1
            try:
                _process_one_invoice(
                    fakturownia_client=fakturownia_client,
                    invoice_number=invoice_number,
                    stats=stats,
                )
            # Resilience boundary: a single invoice failure must not abort the cycle.
            except Exception:
                logger.exception(
                    "Fakturownia patch failed for invoice %s (order %s)",
                    invoice_number,
                    order_id,
                )
                stats["errors"] += 1

    return stats


def _process_one_invoice(
    *,
    fakturownia_client: Any,
    invoice_number: str,
    stats: dict[str, int],
) -> None:
    matches = fakturownia_client.list_invoices(number=invoice_number) or []
    if not matches:
        logger.info("No Fakturownia invoice for number %s — skipping", invoice_number)
        stats["skipped_no_fakturownia_match"] += 1
        return

    if len(matches) > 1:
        # Ambiguous match — nie ryzykujemy patchowania złej faktury. Zgłaszamy jako błąd,
        # żeby operator dostał sygnał; nie łapiemy w skipped_no_fakturownia_match, bo to
        # zupełnie inna sytuacja (znaleźmy więcej faktur o tym samym numerze).
        ids = [m.get("id") for m in matches]
        logger.error(
            "Ambiguous Fakturownia match for number %s: %d invoices (%s) — skipping",
            invoice_number,
            len(matches),
            ids,
        )
        stats["skipped_ambiguous_match"] += 1
        return

    match = matches[0]
    invoice_id = int(match["id"])

    # Fetch full invoice (with positions + settlement_positions).
    invoice = fakturownia_client.get_invoice(invoice_id)

    # Idempotency check.
    if fakturownia_client.has_settlement_with_description(invoice, KAUCJA_DESCRIPTION):
        logger.info("Invoice %s already has kaucja settlement — skipping", invoice_number)
        stats["skipped_already_patched"] += 1
        return

    pet = count_pet_bottles(invoice.get("positions") or [])
    if pet <= 0:
        logger.info("Invoice %s has 0 PET bottles — no kaucja to add", invoice_number)
        stats["skipped_no_pet"] += 1
        return

    amount = _compute_kaucja_amount(pet)
    fakturownia_client.add_settlement_position(
        invoice_id=invoice_id,
        kind="charge",
        amount_pln=amount,
        description=KAUCJA_DESCRIPTION,
    )
    logger.info(
        "Patched invoice %s with kaucja %s PLN (%d PET bottles)",
        invoice_number,
        amount,
        pet,
    )
    stats["patched"] += 1
