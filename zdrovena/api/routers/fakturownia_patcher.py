"""zdrovena.api.routers.fakturownia_patcher — kaucja patcher for Allegro invoices.

Fakturownia's Allegro integration does NOT auto-compute kaucja (deposit) for PET
bottles — invoices come out without a deposit line. This worker fills that gap:

1. Enumerate Allegro-sourced drafts from shipping_store
2. For each order → fetch invoices via AllegroClient.list_order_invoices
   and fetch the full order via AllegroClient.get_order
3. For each Allegro invoice with a Fakturownia-linked number:
   a. GET invoice from Fakturownia (positions + existing settlements)
   b. Compute kaucja from the NATIVE Allegro deposit
      (zdrovena.common.kaucja.calculate_kaucja) — the ONE canonical source,
      shared with allegro_invoice_mapper, so both paths agree to the grosz.
   c. If invoice already has settlement with description "Kaucja za opakowania zwrotne"
      → skip (idempotent)
   d. Else → PUT settlement_positions with new charge row

Kaucja source (PR-13/PR-27): the amount comes ONLY from Allegro's native
per-line ``deposit`` field. The legacy bottles-name heuristic
(count_pet_bottles × KAUCJA_UNIT_PRICE_PLN) is NO LONGER the amount source —
it is kept solely as a cross-check that logs a WARNING when it disagrees
with the native amount, so we can spot mis-tagged products without letting
a heuristic drive the money on the invoice.

Idempotency: uses the Fakturownia settlement description as the marker.

Environment vars:
    KAUCJA_UNIT_PRICE_PLN  default "0.50" (cross-check heuristic only)
"""

from __future__ import annotations

import logging
import os
from decimal import Decimal
from typing import Any

from zdrovena.common.bottles import count_pet_bottles
from zdrovena.common.events import log_event
from zdrovena.common.fakturownia import KAUCJA_DESCRIPTION
from zdrovena.common.kaucja import calculate_kaucja

logger = logging.getLogger(__name__)


# ── constants (env-overridable) ──────────────────────────────────────────────

# Cross-check heuristic only — NOT the amount source (see module docstring).
KAUCJA_UNIT_PRICE_PLN = os.getenv("KAUCJA_UNIT_PRICE_PLN", "0.50").strip() or "0.50"


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
    """Legacy bottles heuristic (cross-check only): pet_count × KAUCJA_UNIT_PRICE_PLN.

    NOT the amount written to the invoice — the invoice amount comes from the
    native Allegro deposit (see module docstring / calculate_kaucja). Used
    only to detect and log divergence.
    """
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

        invoices = allegro_invoices or []
        if not invoices:
            continue

        # Fetch the full order once — its native per-line `deposit` is the
        # canonical kaucja source (calculate_kaucja). Fetched lazily, only
        # when the order actually has invoices to patch.
        try:
            allegro_order = allegro_client.get_order(order_id)
        # Resilience boundary: a single order failure must not abort the cycle.
        except Exception:
            logger.exception("Allegro get_order(%s) failed", order_id)
            stats["errors"] += 1
            continue

        for allegro_inv in invoices:
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
                    allegro_order=allegro_order,
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


def _crosscheck_bottles_heuristic(
    *, invoice: dict[str, Any], invoice_number: str, kaucja: Decimal
) -> None:
    """Compare the legacy bottles heuristic against the canonical native amount.

    Emits a structured WARNING (with correlation_id) on divergence. Never
    changes the amount — this is observability only, so mis-tagged products
    surface without a heuristic driving the invoice total.
    """
    try:
        pet = count_pet_bottles(invoice.get("positions") or [])
        heuristic = Decimal(_compute_kaucja_amount(pet))
    except Exception:
        # Cross-check must never break the patch path.
        return
    if heuristic != kaucja:
        log_event(
            "kaucja_source_divergence",
            level=logging.WARNING,
            invoice_number=invoice_number,
            native_kaucja=f"{kaucja:.2f}",
            heuristic_kaucja=f"{heuristic:.2f}",
            pet_bottles=pet,
            note="native Allegro deposit is authoritative; heuristic is cross-check only",
        )


def _process_one_invoice(
    *,
    fakturownia_client: Any,
    invoice_number: str,
    stats: dict[str, int],
    allegro_order: dict[str, Any],
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

    # Canonical amount: native Allegro deposit (× quantity), shared with the
    # mapper via calculate_kaucja. The bottles heuristic is only a cross-check.
    kaucja = calculate_kaucja(allegro_order)
    _crosscheck_bottles_heuristic(invoice=invoice, invoice_number=invoice_number, kaucja=kaucja)

    if kaucja <= 0:
        logger.info("Invoice %s has no native Allegro deposit — no kaucja to add", invoice_number)
        stats["skipped_no_pet"] += 1
        return

    amount = f"{kaucja:.2f}"
    fakturownia_client.add_settlement_position(
        invoice_id=invoice_id,
        kind="charge",
        amount_pln=amount,
        description=KAUCJA_DESCRIPTION,
    )
    logger.info(
        "Patched invoice %s with kaucja %s PLN (native Allegro deposit)",
        invoice_number,
        amount,
    )
    stats["patched"] += 1
