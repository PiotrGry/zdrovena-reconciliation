"""Tests for zdrovena.common.allegro_invoice_mapper.

Pure function: Allegro checkout-form order → Fakturownia create_invoice() payload.
No I/O — every case is a plain dict-in, dict-out (or None) assertion.
"""

from __future__ import annotations

from zdrovena.common.allegro_invoice_mapper import allegro_order_to_fakturownia_invoice


def _order(**overrides) -> dict:
    base = {
        "id": "af1",
        "buyer": {
            "email": "buyer@allegromail.pl",
            "firstName": "Anna",
            "lastName": "Nowak",
        },
        "invoice": {"required": True, "address": None},
        "lineItems": [
            {
                "offer": {"name": "HUMIO - Alkaliczna Woda Humusowa 500ml x 12"},
                "quantity": 2,
                "price": {"amount": "73.00", "currency": "PLN"},
                "tax": {"rate": "8.00"},
                "deposit": {"price": {"amount": "6.00"}},
            }
        ],
    }
    base.update(overrides)
    return base


class TestInvoiceNotRequired:
    def test_returns_none_when_invoice_not_required(self):
        order = _order(invoice={"required": False, "address": None})
        assert allegro_order_to_fakturownia_invoice(order) is None

    def test_returns_none_when_invoice_key_missing(self):
        order = _order()
        del order["invoice"]
        assert allegro_order_to_fakturownia_invoice(order) is None


class TestPrivateBuyer:
    def test_maps_buyer_name_and_email(self):
        order = _order()
        invoice = allegro_order_to_fakturownia_invoice(order)
        assert invoice["buyer_first_name"] == "Anna"
        assert invoice["buyer_last_name"] == "Nowak"
        assert invoice["buyer_email"] == "buyer@allegromail.pl"
        assert invoice["buyer_company"] == "0"

    def test_oid_is_allegro_order_id(self):
        order = _order()
        invoice = allegro_order_to_fakturownia_invoice(order)
        assert invoice["oid"] == "af1"
        assert invoice["oid_unique"] == "yes"

    def test_kind_is_vat(self):
        assert allegro_order_to_fakturownia_invoice(_order())["kind"] == "vat"


class TestCompanyBuyer:
    def test_maps_company_name_and_tax_no(self):
        order = _order(
            invoice={
                "required": True,
                "address": {
                    "company": {"name": "Nazwa Firmy Sp. z o.o.", "taxId": "5252674798"}
                },
            }
        )
        invoice = allegro_order_to_fakturownia_invoice(order)
        assert invoice["buyer_name"] == "Nazwa Firmy Sp. z o.o."
        assert invoice["buyer_tax_no"] == "5252674798"
        assert invoice["buyer_company"] == "1"
        assert "buyer_first_name" not in invoice


class TestPositionsAndDeposit:
    def test_position_uses_actual_price_not_original(self):
        order = _order()
        invoice = allegro_order_to_fakturownia_invoice(order)
        pos = invoice["positions"][0]
        assert pos["name"] == "HUMIO - Alkaliczna Woda Humusowa 500ml x 12"
        assert pos["quantity"] == 2
        assert pos["total_price_gross"] == 73.00
        assert pos["tax"] == 8

    def test_deposit_becomes_settlement_position_charge(self):
        order = _order()
        invoice = allegro_order_to_fakturownia_invoice(order)
        assert invoice["settlement_positions"] == [
            {"kind": "charge", "amount": "6.00", "description": "Kaucja za opakowania zwrotne"}
        ]

    def test_no_settlement_positions_key_when_no_deposit(self):
        order = _order()
        order["lineItems"][0].pop("deposit")
        invoice = allegro_order_to_fakturownia_invoice(order)
        assert "settlement_positions" not in invoice

    def test_multiple_lines_deposits_summed(self):
        order = _order()
        order["lineItems"].append(
            {
                "offer": {"name": "HUMIO 500ml x 6"},
                "quantity": 1,
                "price": {"amount": "40.00", "currency": "PLN"},
                "tax": {"rate": "8.00"},
                "deposit": {"price": {"amount": "3.00"}},
            }
        )
        invoice = allegro_order_to_fakturownia_invoice(order)
        assert len(invoice["positions"]) == 2
        assert invoice["settlement_positions"] == [
            {"kind": "charge", "amount": "9.00", "description": "Kaucja za opakowania zwrotne"}
        ]

    def test_tax_rate_converted_to_integer_percent(self):
        order = _order()
        order["lineItems"][0]["tax"]["rate"] = "23.00"
        invoice = allegro_order_to_fakturownia_invoice(order)
        assert invoice["positions"][0]["tax"] == 23
