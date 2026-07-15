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

Both `lineItems[i].price.amount` and `lineItems[i].deposit.price.amount`
are PER-UNIT values in Allegro's schema, not line totals — confirmed
against a real production order (quantity=2, price.amount="73.00",
deposit.price.amount="6.00") whose `summary.totalToPay` of "158.00" only
matches `(73.00 + 6.00) * 2`, not `73.00 + 6.00`. Both fields are
multiplied by `quantity` below.

Every order gets a Fakturownia invoice, whether or not the buyer explicitly
requested a VAT invoice (`invoice.required`). Allegro lets buyers opt for a
receipt/paragon instead of a VAT invoice, but this business still issues a
Fakturownia invoice for that sale — just addressed to the buyer as a private
individual (no NIP, buyer's own name) instead of a company. When no
invoice-specific address was supplied (`invoice.address` is None — always
true when `invoice.required` is False, and possible even when True), the
buyer's own registered address (`order.buyer.address`) is used as the
billing address instead — never the delivery address, which may be a
locker/pickup point unsuitable for billing. Note: `buyer.address` uses the
key `postCode`, while `invoice.address`/`delivery.address` use `zipCode` —
both are checked.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from zdrovena.common.fakturownia import KAUCJA_DESCRIPTION
from zdrovena.common.kaucja import calculate_kaucja, parse_line_quantity

_CENTS = Decimal("0.01")


def allegro_expected_payable(order: dict[str, Any]) -> Decimal | None:
    """Allegro's own payable total for the invoice, as a ``Decimal``.

    This is ``summary.totalToPay`` minus the delivery cost, because the invoice
    carries no shipping line item. Returns ``None`` when ``totalToPay`` is
    absent or unparseable. Shared by the invoice preview and any parity
    cross-check so both compare against the identical figure (R4.3).
    """
    summary = order.get("summary") or {}
    total_to_pay_raw = (summary.get("totalToPay") or {}).get("amount")
    if total_to_pay_raw is None:
        return None
    delivery_cost_raw = ((order.get("delivery") or {}).get("cost") or {}).get("amount")
    try:
        return Decimal(str(total_to_pay_raw)) - Decimal(str(delivery_cost_raw or "0"))
    except (ArithmeticError, ValueError):
        return None


def allegro_order_to_fakturownia_invoice(order: dict[str, Any]) -> dict[str, Any]:
    invoice_request = order.get("invoice") or {}
    buyer = order.get("buyer") or {}
    address = invoice_request.get("address") or buyer.get("address") or {}
    company = address.get("company") or {}
    is_company = bool(company.get("name"))

    positions: list[dict[str, Any]] = []

    for item in order.get("lineItems") or []:
        offer = item.get("offer") or {}
        quantity = parse_line_quantity(item.get("quantity"))
        unit_price = Decimal(str((item.get("price") or {}).get("amount", "0")))
        tax_rate = Decimal(str((item.get("tax") or {}).get("rate", "23")))
        # Money stays Decimal through the arithmetic; quantize to cents once and
        # only then cross the wire as a float (Fakturownia's position schema
        # expects a number). Quantizing before float() avoids binary-float drift
        # like 3 * 0.1 → 0.30000000000000004.
        line_total = (unit_price * quantity).quantize(_CENTS, rounding=ROUND_HALF_UP)
        positions.append(
            {
                "name": offer.get("name", ""),
                "quantity": quantity,
                "total_price_gross": float(line_total),
                "tax": int(tax_rate),
            }
        )

    # Kaucja — jedno kanoniczne źródło (natywny deposit z Allegro × quantity),
    # współdzielone z patcherem, żeby obie ścieżki liczyły tę samą kwotę.
    deposit_total = calculate_kaucja(order)

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
    zip_code = address.get("zipCode") or address.get("postCode")
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
