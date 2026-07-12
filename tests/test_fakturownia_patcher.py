"""tests/test_fakturownia_patcher.py — kaucja patcher worker.

Worker responsibility:
  1. Enumerate recent Allegro orders from `shipping_store` (source='allegro')
  2. For each order → fetch invoices via AllegroClient.list_order_invoices
     and the full order via AllegroClient.get_order
  3. For each Allegro invoice with a Fakturownia-linked number:
     a. GET invoice from Fakturownia (to read positions + existing settlements)
     b. Compute kaucja from the NATIVE Allegro deposit (calculate_kaucja) —
        the legacy bottles heuristic is NO LONGER the amount source, only a
        cross-check that logs a warning on divergence (PR-13/PR-27)
     c. If invoice already has settlement with description "Kaucja za opakowania zwrotne" → SKIP
     d. Else → PUT settlement_positions with new row

All I/O is mocked. Tests assert:
  - amount == native Allegro deposit (× quantity)
  - idempotency (no double-patch)
  - no native deposit → no patch
  - error in one invoice doesn't block others
  - stats dict shape
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from zdrovena.api.routers.fakturownia_patcher import (
    KAUCJA_DESCRIPTION,
    KAUCJA_UNIT_PRICE_PLN,
    patch_allegro_invoices_once,
)

# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def allegro_client():
    return MagicMock()


@pytest.fixture
def fakturownia_client():
    return MagicMock()


@pytest.fixture
def shipping_store():
    return MagicMock()


def _draft(*, external_order_id: str, status: str = "shipped") -> dict:
    return {
        "id": f"draft-{external_order_id}",
        "source": "allegro",
        "external_order_id": external_order_id,
        "status": status,
    }


def _allegro_order(
    *, order_id: str = "ORD-1", deposit_amount: str | None = "6.00", quantity: int = 1
) -> dict:
    """Minimal Allegro order carrying a native per-line deposit.

    `deposit_amount=None` → no deposit line (e.g. glass-only, no kaucja).
    """
    line: dict = {
        "offer": {"name": "Woda Humio"},
        "quantity": quantity,
        "price": {"amount": "10.00", "currency": "PLN"},
        "tax": {"rate": "8.00"},
    }
    if deposit_amount is not None:
        line["deposit"] = {"price": {"amount": deposit_amount}}
    return {"id": order_id, "lineItems": [line]}


def _fakturownia_invoice(
    *,
    invoice_id: int,
    positions: list[dict] | None = None,
    settlement_positions: list[dict] | None = None,
) -> dict:
    return {
        "id": invoice_id,
        "number": f"FV/2025/{invoice_id}",
        "positions": positions or [],
        "settlement_positions": settlement_positions or [],
    }


# ── happy path ───────────────────────────────────────────────────────────────


class TestHappyPath:
    def test_single_invoice_patched_with_kaucja(
        self, allegro_client, fakturownia_client, shipping_store
    ):
        """1 order → 1 invoice → native deposit 6.00 PLN added as kaucja."""
        shipping_store.list_drafts.return_value = [_draft(external_order_id="ORD-1")]
        allegro_client.list_order_invoices.return_value = [
            {"id": "inv-1", "invoiceNumber": "FV/2025/100", "fileType": "VAT"},
        ]
        allegro_client.get_order.return_value = _allegro_order(
            order_id="ORD-1", deposit_amount="6.00", quantity=1
        )
        fakturownia_client.list_invoices.return_value = [
            _fakturownia_invoice(
                invoice_id=100,
                positions=[
                    {"name": "Woda Humio 500ml x 12", "quantity": 1},
                ],
            )
        ]
        fakturownia_client.get_invoice.return_value = _fakturownia_invoice(
            invoice_id=100,
            positions=[{"name": "Woda Humio 500ml x 12", "quantity": 1}],
        )
        fakturownia_client.has_settlement_with_description.return_value = False

        stats = patch_allegro_invoices_once(
            allegro_client=allegro_client,
            fakturownia_client=fakturownia_client,
            shipping_store=shipping_store,
        )

        assert stats["orders_scanned"] == 1
        assert stats["invoices_scanned"] == 1
        assert stats["patched"] == 1
        assert stats["skipped_already_patched"] == 0
        assert stats["skipped_no_pet"] == 0
        assert stats["errors"] == 0

        # patcher called add_settlement_position with correct args
        fakturownia_client.add_settlement_position.assert_called_once()
        call = fakturownia_client.add_settlement_position.call_args
        assert call.kwargs["invoice_id"] == 100
        assert call.kwargs["kind"] == "charge"
        assert call.kwargs["amount_pln"] == "6.00"  # native Allegro deposit
        assert call.kwargs["description"] == KAUCJA_DESCRIPTION

    def test_multiple_invoices_across_orders(
        self, allegro_client, fakturownia_client, shipping_store
    ):
        shipping_store.list_drafts.return_value = [
            _draft(external_order_id="ORD-1"),
            _draft(external_order_id="ORD-2"),
        ]
        allegro_client.list_order_invoices.side_effect = [
            [{"id": "inv-1", "invoiceNumber": "FV/2025/101"}],
            [{"id": "inv-2", "invoiceNumber": "FV/2025/102"}],
        ]
        allegro_client.get_order.side_effect = [
            _allegro_order(order_id="ORD-1", deposit_amount="6.00", quantity=1),
            _allegro_order(order_id="ORD-2", deposit_amount="18.00", quantity=1),
        ]
        fakturownia_client.list_invoices.side_effect = [
            [
                _fakturownia_invoice(
                    invoice_id=101,
                    positions=[
                        {"name": "Woda Humio 500ml x 6", "quantity": 2},
                    ],
                )
            ],
            [
                _fakturownia_invoice(
                    invoice_id=102,
                    positions=[
                        {"name": "Zgrzewka Humio", "quantity": 3},
                    ],
                )
            ],
        ]
        fakturownia_client.get_invoice.side_effect = [
            _fakturownia_invoice(
                invoice_id=101,
                positions=[
                    {"name": "Woda Humio 500ml x 6", "quantity": 2},
                ],
            ),
            _fakturownia_invoice(
                invoice_id=102,
                positions=[
                    {"name": "Zgrzewka Humio", "quantity": 3},
                ],
            ),
        ]
        fakturownia_client.has_settlement_with_description.return_value = False

        stats = patch_allegro_invoices_once(
            allegro_client=allegro_client,
            fakturownia_client=fakturownia_client,
            shipping_store=shipping_store,
        )

        assert stats["patched"] == 2
        assert fakturownia_client.add_settlement_position.call_count == 2
        amounts = [
            c.kwargs["amount_pln"]
            for c in fakturownia_client.add_settlement_position.call_args_list
        ]
        assert amounts == ["6.00", "18.00"]  # native deposits per order


# ── idempotency ──────────────────────────────────────────────────────────────


class TestIdempotency:
    def test_skip_when_already_patched(self, allegro_client, fakturownia_client, shipping_store):
        shipping_store.list_drafts.return_value = [_draft(external_order_id="ORD-1")]
        allegro_client.list_order_invoices.return_value = [
            {"id": "inv-1", "invoiceNumber": "FV/2025/100"},
        ]
        allegro_client.get_order.return_value = _allegro_order(
            order_id="ORD-1", deposit_amount="6.00", quantity=1
        )
        fakturownia_client.list_invoices.return_value = [
            _fakturownia_invoice(
                invoice_id=100,
                positions=[
                    {"name": "Woda Humio 500ml x 12", "quantity": 1},
                ],
            )
        ]
        fakturownia_client.get_invoice.return_value = _fakturownia_invoice(
            invoice_id=100,
            positions=[{"name": "Woda Humio 500ml x 12", "quantity": 1}],
            settlement_positions=[
                {"kind": "charge", "amount": "6.00", "reason": KAUCJA_DESCRIPTION}
            ],
        )
        fakturownia_client.has_settlement_with_description.return_value = True

        stats = patch_allegro_invoices_once(
            allegro_client=allegro_client,
            fakturownia_client=fakturownia_client,
            shipping_store=shipping_store,
        )

        assert stats["patched"] == 0
        assert stats["skipped_already_patched"] == 1
        fakturownia_client.add_settlement_position.assert_not_called()


# ── no deposit (glass-only) ──────────────────────────────────────────────────


class TestNoDeposit:
    def test_no_native_deposit_not_patched(
        self, allegro_client, fakturownia_client, shipping_store
    ):
        shipping_store.list_drafts.return_value = [_draft(external_order_id="ORD-1")]
        allegro_client.list_order_invoices.return_value = [
            {"id": "inv-1", "invoiceNumber": "FV/2025/100"},
        ]
        # Order has NO native deposit (e.g. glass) → no kaucja.
        allegro_client.get_order.return_value = _allegro_order(
            order_id="ORD-1", deposit_amount=None
        )
        fakturownia_client.list_invoices.return_value = [
            _fakturownia_invoice(
                invoice_id=100,
                positions=[
                    {"name": "Woda Humio w szkle 500ml x 12", "quantity": 1},
                ],
            )
        ]
        fakturownia_client.get_invoice.return_value = _fakturownia_invoice(
            invoice_id=100,
            positions=[{"name": "Woda Humio w szkle 500ml x 12", "quantity": 1}],
        )
        fakturownia_client.has_settlement_with_description.return_value = False

        stats = patch_allegro_invoices_once(
            allegro_client=allegro_client,
            fakturownia_client=fakturownia_client,
            shipping_store=shipping_store,
        )

        assert stats["patched"] == 0
        assert stats["skipped_no_pet"] == 1
        fakturownia_client.add_settlement_position.assert_not_called()


# ── errors don't block ───────────────────────────────────────────────────────


class TestErrorIsolation:
    def test_error_on_one_invoice_does_not_block_others(
        self, allegro_client, fakturownia_client, shipping_store
    ):
        shipping_store.list_drafts.return_value = [
            _draft(external_order_id="ORD-1"),
            _draft(external_order_id="ORD-2"),
        ]
        allegro_client.list_order_invoices.side_effect = [
            RuntimeError("Allegro API down"),  # 1st call fails
            [{"id": "inv-2", "invoiceNumber": "FV/2025/102"}],  # 2nd OK
        ]
        # get_order only reached for ORD-2 (ORD-1 failed at list_order_invoices).
        allegro_client.get_order.return_value = _allegro_order(
            order_id="ORD-2", deposit_amount="6.00", quantity=1
        )
        fakturownia_client.list_invoices.return_value = [
            _fakturownia_invoice(
                invoice_id=102,
                positions=[
                    {"name": "Woda Humio 500ml x 12", "quantity": 1},
                ],
            )
        ]
        fakturownia_client.get_invoice.return_value = _fakturownia_invoice(
            invoice_id=102,
            positions=[{"name": "Woda Humio 500ml x 12", "quantity": 1}],
        )
        fakturownia_client.has_settlement_with_description.return_value = False

        stats = patch_allegro_invoices_once(
            allegro_client=allegro_client,
            fakturownia_client=fakturownia_client,
            shipping_store=shipping_store,
        )

        assert stats["errors"] == 1
        assert stats["patched"] == 1

    def test_error_on_patch_recorded_but_doesnt_stop(
        self, allegro_client, fakturownia_client, shipping_store
    ):
        shipping_store.list_drafts.return_value = [
            _draft(external_order_id="ORD-1"),
            _draft(external_order_id="ORD-2"),
        ]
        allegro_client.list_order_invoices.side_effect = [
            [{"id": "inv-1", "invoiceNumber": "FV/2025/101"}],
            [{"id": "inv-2", "invoiceNumber": "FV/2025/102"}],
        ]
        allegro_client.get_order.side_effect = [
            _allegro_order(order_id="ORD-1", deposit_amount="6.00", quantity=1),
            _allegro_order(order_id="ORD-2", deposit_amount="6.00", quantity=1),
        ]
        fakturownia_client.list_invoices.side_effect = [
            [
                _fakturownia_invoice(
                    invoice_id=101,
                    positions=[
                        {"name": "Woda Humio 500ml x 12", "quantity": 1},
                    ],
                )
            ],
            [
                _fakturownia_invoice(
                    invoice_id=102,
                    positions=[
                        {"name": "Woda Humio 500ml x 12", "quantity": 1},
                    ],
                )
            ],
        ]
        fakturownia_client.get_invoice.side_effect = [
            _fakturownia_invoice(
                invoice_id=101,
                positions=[
                    {"name": "Woda Humio 500ml x 12", "quantity": 1},
                ],
            ),
            _fakturownia_invoice(
                invoice_id=102,
                positions=[
                    {"name": "Woda Humio 500ml x 12", "quantity": 1},
                ],
            ),
        ]
        fakturownia_client.has_settlement_with_description.return_value = False
        # 1st patch fails, 2nd succeeds
        fakturownia_client.add_settlement_position.side_effect = [
            RuntimeError("Fakturownia 500"),
            {"id": 102},
        ]

        stats = patch_allegro_invoices_once(
            allegro_client=allegro_client,
            fakturownia_client=fakturownia_client,
            shipping_store=shipping_store,
        )

        assert stats["errors"] == 1
        assert stats["patched"] == 1


# ── invoice-number matching (Allegro invoice → Fakturownia invoice) ─────────


class TestInvoiceMatching:
    def test_matches_fakturownia_invoice_by_number(
        self, allegro_client, fakturownia_client, shipping_store
    ):
        """Allegro's list_order_invoices returns invoice with `invoiceNumber`;
        we must query Fakturownia by that number to find the corresponding
        Fakturownia invoice id."""
        shipping_store.list_drafts.return_value = [_draft(external_order_id="ORD-1")]
        allegro_client.list_order_invoices.return_value = [
            {"id": "allegro-inv-1", "invoiceNumber": "FV/2025/999"},
        ]
        allegro_client.get_order.return_value = _allegro_order(
            order_id="ORD-1", deposit_amount="6.00", quantity=1
        )
        # Fakturownia returns invoice matching that number
        fakturownia_client.list_invoices.return_value = [
            _fakturownia_invoice(
                invoice_id=999,
                positions=[
                    {"name": "Woda Humio 500ml x 12", "quantity": 1},
                ],
            ),
        ]
        fakturownia_client.get_invoice.return_value = _fakturownia_invoice(
            invoice_id=999,
            positions=[{"name": "Woda Humio 500ml x 12", "quantity": 1}],
        )
        fakturownia_client.has_settlement_with_description.return_value = False

        patch_allegro_invoices_once(
            allegro_client=allegro_client,
            fakturownia_client=fakturownia_client,
            shipping_store=shipping_store,
        )

        # verifies we searched Fakturownia by the Allegro invoice's number
        fakturownia_client.list_invoices.assert_called_with(number="FV/2025/999")

    def test_skips_when_no_fakturownia_match(
        self, allegro_client, fakturownia_client, shipping_store
    ):
        shipping_store.list_drafts.return_value = [_draft(external_order_id="ORD-1")]
        allegro_client.list_order_invoices.return_value = [
            {"id": "allegro-inv-1", "invoiceNumber": "FV/2025/999"},
        ]
        allegro_client.get_order.return_value = _allegro_order(
            order_id="ORD-1", deposit_amount="6.00", quantity=1
        )
        fakturownia_client.list_invoices.return_value = []  # not found

        stats = patch_allegro_invoices_once(
            allegro_client=allegro_client,
            fakturownia_client=fakturownia_client,
            shipping_store=shipping_store,
        )

        assert stats["skipped_no_fakturownia_match"] == 1
        fakturownia_client.add_settlement_position.assert_not_called()

    def test_ambiguous_match_skipped(self, allegro_client, fakturownia_client, shipping_store):
        """Więcej niż 1 faktura o tym samym numerze — NIE patchujemy żadnej
        (nie ryzykujemy dopisania kaucji do złej). Zliczane w skipped_ambiguous_match."""
        shipping_store.list_drafts.return_value = [_draft(external_order_id="ORD-1")]
        allegro_client.list_order_invoices.return_value = [
            {"id": "allegro-inv-1", "invoiceNumber": "FV/2025/999"},
        ]
        allegro_client.get_order.return_value = _allegro_order(
            order_id="ORD-1", deposit_amount="6.00", quantity=1
        )
        # Dwie różne faktury zwrócone dla tego samego numeru (patologia, ale możliwa)
        fakturownia_client.list_invoices.return_value = [
            _fakturownia_invoice(invoice_id=111, positions=[]),
            _fakturownia_invoice(invoice_id=222, positions=[]),
        ]

        stats = patch_allegro_invoices_once(
            allegro_client=allegro_client,
            fakturownia_client=fakturownia_client,
            shipping_store=shipping_store,
        )

        assert stats["skipped_ambiguous_match"] == 1
        assert stats["patched"] == 0
        # KLUCZOWE: nie pobieramy szczegółów żadnej z faktur ani nie patchujemy
        fakturownia_client.get_invoice.assert_not_called()
        fakturownia_client.add_settlement_position.assert_not_called()


# ── kaucja source cross-check (native vs heuristic) ──────────────────────────


class TestCrossCheck:
    def test_amount_is_native_deposit_not_bottles_heuristic(
        self, allegro_client, fakturownia_client, shipping_store, caplog
    ):
        """Native Allegro deposit is authoritative even when it disagrees with
        the bottles×0.50 heuristic. Here: invoice names imply 12 PET → heuristic
        6.00, but native deposit is 7.00 → amount MUST be 7.00, and a divergence
        warning is logged (never blocks)."""
        import logging

        shipping_store.list_drafts.return_value = [_draft(external_order_id="ORD-1")]
        allegro_client.list_order_invoices.return_value = [
            {"id": "inv-1", "invoiceNumber": "FV/2025/100"},
        ]
        allegro_client.get_order.return_value = _allegro_order(
            order_id="ORD-1", deposit_amount="7.00", quantity=1
        )
        fakturownia_client.list_invoices.return_value = [
            _fakturownia_invoice(
                invoice_id=100,
                positions=[{"name": "Woda Humio 500ml x 12", "quantity": 1}],
            )
        ]
        fakturownia_client.get_invoice.return_value = _fakturownia_invoice(
            invoice_id=100,
            positions=[{"name": "Woda Humio 500ml x 12", "quantity": 1}],
        )
        fakturownia_client.has_settlement_with_description.return_value = False

        with caplog.at_level(logging.WARNING, logger="zdrovena.events"):
            patch_allegro_invoices_once(
                allegro_client=allegro_client,
                fakturownia_client=fakturownia_client,
                shipping_store=shipping_store,
            )

        call = fakturownia_client.add_settlement_position.call_args
        assert call.kwargs["amount_pln"] == "7.00"  # native wins, not 6.00
        assert any("kaucja_source_divergence" in r.message for r in caplog.records)


# ── source filter (only Allegro orders) ──────────────────────────────────────


class TestSourceFilter:
    def test_shopify_drafts_ignored(self, allegro_client, fakturownia_client, shipping_store):
        shipping_store.list_drafts.return_value = [
            {"id": "d1", "source": "shopify", "external_order_id": "SHOP-1"},
            {"id": "d2", "source": "allegro", "external_order_id": "ORD-1", "status": "shipped"},
        ]
        allegro_client.list_order_invoices.return_value = []

        stats = patch_allegro_invoices_once(
            allegro_client=allegro_client,
            fakturownia_client=fakturownia_client,
            shipping_store=shipping_store,
        )

        assert stats["orders_scanned"] == 1
        # allegro client called exactly once, for ORD-1
        allegro_client.list_order_invoices.assert_called_once_with("ORD-1")
        # No invoices → order never fetched.
        allegro_client.get_order.assert_not_called()


# ── constants sanity ─────────────────────────────────────────────────────────


class TestConstants:
    def test_kaucja_unit_price_is_0_50(self):
        # can be overridden via env var, default 0.50 (cross-check heuristic only)
        assert KAUCJA_UNIT_PRICE_PLN == "0.50"

    def test_kaucja_description_is_polish(self):
        assert "Kaucja" in KAUCJA_DESCRIPTION or "kaucja" in KAUCJA_DESCRIPTION.lower()
