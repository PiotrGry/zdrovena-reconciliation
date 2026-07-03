"""zdrovena.common.allegro_invoice_mapper — Allegro order → Fakturownia invoice.

Maps one Allegro checkout-form order (the shape returned by
AllegroClient.list_orders() / used by allegro_poller.py) into the payload
expected by FakturowniaClient.create_invoice().

Kaucja (deposit) is read directly from Allegro's native per-line
`deposit.price.amount` field and folded into `settlement_positions` on the
SAME invoice at creation time — unlike the Shopify flow (which detects
kaucja via a "kaucja" substring in the line item title, because Shopify has
no native deposit concept), Allegro models deposits structurally, so no
heuristic matching is needed here.

Returns None when the buyer did not request a VAT invoice at all
(`invoice.required` is False or missing) — Allegro lets buyers opt for a
receipt/paragon instead, which is a normal case, not an error.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from zdrovena.common.fakturownia import KAUCJA_DESCRIPTION


def allegro_order_to_fakturownia_invoice(order: dict[str, Any]) -> dict[str, Any] | None:
    invoice_request = order.get("invoice") or {}
    if not invoice_request.get("required"):
        return None

    buyer = order.get("buyer") or {}
    address = invoice_request.get("address") or {}
    company = address.get("company") or {}
    is_company = bool(company.get("name"))

    positions: list[dict[str, Any]] = []
    deposit_total = Decimal("0")

    for item in order.get("lineItems") or []:
        offer = item.get("offer") or {}
        quantity = int(item.get("quantity", 1) or 1)
        price = Decimal(str((item.get("price") or {}).get("amount", "0")))
        tax_rate = Decimal(str((item.get("tax") or {}).get("rate", "23")))
        positions.append(
            {
                "name": offer.get("name", ""),
                "quantity": quantity,
                "total_price_gross": float(price),
                "tax": int(tax_rate),
            }
        )
        deposit = item.get("deposit")
        if deposit:
            # ASSUMPTION (unverified against a real multi-quantity deposit
            # order as of this writing): deposit.price.amount is the TOTAL
            # deposit for this line (matching how price.amount is also a
            # line total), not a per-unit amount. If a real order shows
            # otherwise, this needs `* quantity`. See the manual verification
            # step in docs/superpowers/plans/2026-07-03-allegro-invoice-creation.md
            # (Task 8) before trusting this on a multi-quantity deposit order.
            deposit_total += Decimal(str((deposit.get("price") or {}).get("amount", "0")))

    invoice: dict[str, Any] = {
        "kind": "vat",
        "oid": str(order.get("id") or ""),
        "oid_unique": "yes",
        "positions": positions,
    }

    if is_company:
        invoice["buyer_name"] = company.get("name", "")
        invoice["buyer_company"] = "1"
        tax_no = company.get("taxId")
        if tax_no:
            invoice["buyer_tax_no"] = tax_no
    else:
        invoice["buyer_first_name"] = buyer.get("firstName", "")
        invoice["buyer_last_name"] = buyer.get("lastName", "")
        invoice["buyer_company"] = "0"

    invoice["buyer_email"] = buyer.get("email", "")

    street = address.get("street")
    if street:
        invoice["buyer_street"] = street
    city = address.get("city")
    if city:
        invoice["buyer_city"] = city
    zip_code = address.get("zipCode")
    if zip_code:
        invoice["buyer_post_code"] = zip_code
    country_code = address.get("countryCode")
    if country_code:
        invoice["buyer_country"] = country_code

    if deposit_total > 0:
        invoice["settlement_positions"] = [
            {
                "kind": "charge",
                "amount": f"{deposit_total:.2f}",
                "description": KAUCJA_DESCRIPTION,
            }
        ]

    return invoice
