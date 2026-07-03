"""Contract tests pinning our Fakturownia parsers to REAL production fixtures.

Fixtures under tests/fixtures/fakturownia/ are genuine production responses and are
treated as ground truth. The invoice-detail fixture is invoice "Z1" (an advance /
zaliczkowa invoice whose free-text description references the order's kaucja).
"""

from __future__ import annotations

import json
from pathlib import Path

from zdrovena.api.routers.fakturownia_patcher import _compute_kaucja_amount
from zdrovena.common.bottles import count_pet_bottles

_FIXTURES = Path(__file__).parent / "fixtures" / "fakturownia"


def _load(name: str):
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


class TestFakturowniaInvoicesList:
    def test_parse_invoices_list(self):
        invoices = _load("invoices-list.json")

        assert isinstance(invoices, list)
        assert len(invoices) == 25
        for inv in invoices:
            for field in ("id", "number", "sell_date", "price_gross", "status", "kind"):
                assert field in inv, f"missing {field!r} in invoice"


class TestFakturowniaInvoiceDetailKaucja:
    def test_parse_invoice_detail_z1_kaucja(self):
        inv = _load("invoice-detail.json")

        assert inv["number"] == "Z1"
        assert inv["kind"] == "advance"

        # Structure uses `positions` (NOT `items`).
        assert "positions" in inv
        assert "items" not in inv
        assert len(inv["positions"]) >= 1

        # Free-text kaucja note lives on `description`.
        assert "kaucja w wysokości 2508,00 PLN" in inv["description"]


class TestFakturowniaKaucjaPositionsParseable:
    def test_fakturownia_kaucja_positions_parseable(self):
        inv = _load("invoice-detail.json")
        positions = inv["positions"]

        pet = count_pet_bottles(positions)
        # Position: "... 12 butelek ..." × quantity 418 → 5016 PET bottles.
        assert pet == 5016

        # Kaucja computed from positions must match the amount stated in the
        # invoice's own description ("2508,00 PLN") — this is the contract that
        # binds our parser to the real invoice.
        assert _compute_kaucja_amount(pet) == "2508.00"
