"""zdrovena.api.routers.allegro_invoicer — create + push a Fakturownia
invoice for an Allegro order.

Replaces reliance on Fakturownia's Allegro app-store integration, which
does not compute kaucja (deposit) and which this business does not control.
Instead of patching an already-wrong invoice after the fact, this creates
the invoice correctly the first time: the base invoice (positions only) via
POST /invoices.json, then kaucja (if any — from allegro_invoice_mapper's
settlement_positions) via a follow-up add_settlement_position() PUT, since
Fakturownia's create endpoint rejects an inline settlement_positions block
(confirmed against the live API: 422 "Nieprawidłowy atrybut: 'description'"
— that shape is only valid on the PUT-based update path). The final PDF is
only fetched after the settlement position is attached, so the copy pushed
to Allegro already includes the kaucja line.

Idempotency: before creating anything, checks Fakturownia for an existing
invoice with oid=<allegro_order_id> (Fakturownia is the source of truth —
no separate local state to keep in sync).

IMPORTANT — do not turn this into a retry loop: if invoice creation fails,
this does NOT mark anything as "already attempted" locally. The caller
(allegro_poller.py) only invokes this once per order, at the same point it
creates the shipping draft, and relies on the existing
_existing_active_allegro_draft() check to avoid re-processing an order that
already has an active draft. If you call this function from a new call site
without an equivalent guard, a persistently-failing order will alert on
every call.

Logging & alerting: every failure logs at ERROR (auto-forwarded to Azure
Application Insights via the OpenTelemetry wiring in api/main.py) AND sends
exactly one SMS via sms_service — a missing/wrong invoice is a compliance
problem, not just an operational one, so it must not silently disappear
into logs.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any

from zdrovena.common.allegro_invoice_mapper import allegro_order_to_fakturownia_invoice
from zdrovena.common.correlation import get_correlation_id
from zdrovena.common.secrets import get_secret

logger = logging.getLogger("zdrovena.api.routers.allegro_invoicer")


def _alert_invoice_failure(*, allegro_order_id: str, reason: str) -> None:
    token = get_secret("smsapi_token", required=False)
    notify_phone = get_secret("notify_phone", required=False)
    if not token or not notify_phone:
        return
    try:
        from zdrovena.common.sms_service import send_invoice_failure_sms

        send_invoice_failure_sms(
            notify_phone=notify_phone,
            allegro_order_id=allegro_order_id,
            reason=reason,
            token=token,
        )
    except Exception:
        # Resilience boundary: alerting must never raise into the caller —
        # the ERROR log above already captured the real failure.
        logger.exception("Invoice-failure SMS alert itself failed for order %s", allegro_order_id)


_TOTAL_MISMATCH_TOLERANCE = Decimal("0.01")


def _check_total_matches_allegro(order: dict[str, Any], payload: dict[str, Any]) -> None:
    """Sanity check: compare our computed invoice total against Allegro's own
    order.summary.totalToPay (minus delivery cost, since the invoice does not
    include shipping as a line item). Logs a WARNING on mismatch — does NOT
    raise and does NOT block invoice creation, since a false positive
    shouldn't halt legitimate invoicing, and does NOT alert via SMS (this is
    a lower-severity signal for later investigation, not an active failure).

    Added after a real bug (allegro_invoice_mapper.py not multiplying
    price/deposit by quantity) was caught by exactly this kind of manual
    cross-check against a real order's totalToPay — this automates that
    check going forward.
    """
    try:
        total_to_pay_amount = ((order.get("summary") or {}).get("totalToPay") or {}).get("amount")
        if total_to_pay_amount is None:
            return

        delivery_cost_amount = ((order.get("delivery") or {}).get("cost") or {}).get("amount", "0")

        expected = Decimal(str(total_to_pay_amount)) - Decimal(str(delivery_cost_amount))
        positions_sum = sum(
            (Decimal(str(p["total_price_gross"])) for p in payload.get("positions", [])),
            start=Decimal("0"),
        )
        settlements_sum = sum(
            (Decimal(s["amount"]) for s in payload.get("settlement_positions", [])),
            start=Decimal("0"),
        )
        computed = positions_sum + settlements_sum
    except (AttributeError, InvalidOperation, KeyError, TypeError, ValueError):
        return

    if abs(computed - expected) > _TOTAL_MISMATCH_TOLERANCE:
        # Log every component separately (positions vs settlements/kaucja vs
        # Allegro's totalToPay) plus the correlation_id, so an operator can see
        # exactly which part drifted without re-deriving the sums by hand.
        logger.warning(
            "Invoice total mismatch for Allegro order %s [correlation_id=%s]: "
            "computed invoice total %s (positions=%s + settlements/kaucja=%s) "
            "does not match Allegro's totalToPay-minus-delivery %s "
            "(totalToPay=%s, delivery=%s) — proceeding anyway, but this may indicate "
            "a bug in allegro_invoice_mapper.py",
            order.get("id"),
            get_correlation_id(),
            computed,
            positions_sum,
            settlements_sum,
            expected,
            total_to_pay_amount,
            delivery_cost_amount,
        )


def create_invoice_for_order(
    order: dict[str, Any],
    *,
    fakturownia_client: Any,
    allegro_client: Any,
) -> dict[str, Any]:
    """Create a Fakturownia invoice for one Allegro order and push it back.

    Every order gets an invoice — allegro_order_to_fakturownia_invoice()
    always returns a payload, addressed to the buyer as a private
    individual when they did not explicitly request a VAT invoice.

    Returns a dict with at least a "status" key:
      "already_exists" — Fakturownia already has an invoice for this order
      "created"        — success; also has fakturownia_invoice_id/number
      "error"          — also has "error" (str); fakturownia_invoice_id is
                          present if Fakturownia succeeded but the Allegro
                          push failed
    """
    allegro_order_id = str(order.get("id") or "")

    payload = allegro_order_to_fakturownia_invoice(order)

    try:
        existing = fakturownia_client.list_invoices(oid=allegro_order_id)
    except Exception as exc:
        logger.exception(
            "Fakturownia list_invoices lookup failed for Allegro order %s", allegro_order_id
        )
        _alert_invoice_failure(allegro_order_id=allegro_order_id, reason=str(exc))
        return {"status": "error", "error": str(exc)}

    if existing:
        # Recover the existing invoice's id/number so the caller can persist it
        # instead of resetting local state to None and looping (the 502 bug).
        # Fakturownia is the source of truth; oid+oid_unique guarantees at most
        # one match, so existing[0] is canonical.
        existing_invoice = existing[0]
        logger.info(
            "Fakturownia already has an invoice for Allegro order %s (id=%s) — recovering id",
            allegro_order_id,
            existing_invoice.get("id"),
        )
        return {
            "status": "already_exists",
            "fakturownia_invoice_id": existing_invoice.get("id"),
            "fakturownia_invoice_number": existing_invoice.get("number"),
        }

    _check_total_matches_allegro(order, payload)

    settlement_positions = payload.get("settlement_positions") or []
    invoice_body = {k: v for k, v in payload.items() if k != "settlement_positions"}

    try:
        created = fakturownia_client.create_invoice(invoice_body)
        fakturownia_invoice_id = created["id"]
        fakturownia_invoice_number = created["number"]
    except Exception as exc:
        logger.exception("Fakturownia create_invoice failed for Allegro order %s", allegro_order_id)
        _alert_invoice_failure(allegro_order_id=allegro_order_id, reason=str(exc))
        return {"status": "error", "error": str(exc)}

    try:
        for settlement in settlement_positions:
            fakturownia_client.add_settlement_position(
                invoice_id=fakturownia_invoice_id,
                kind=settlement["kind"],
                amount_pln=settlement["amount"],
                description=settlement["description"],
            )
    except Exception as exc:
        logger.exception(
            "Adding settlement position to Fakturownia invoice %s failed for order %s",
            fakturownia_invoice_id,
            allegro_order_id,
        )
        _alert_invoice_failure(allegro_order_id=allegro_order_id, reason=str(exc))
        return {
            "status": "error",
            "error": str(exc),
            "fakturownia_invoice_id": fakturownia_invoice_id,
        }

    try:
        pdf_bytes = fakturownia_client.get_invoice_pdf(fakturownia_invoice_id)
        declaration = allegro_client.create_invoice_declaration(
            order_id=allegro_order_id, invoice_number=fakturownia_invoice_number
        )
        allegro_client.upload_invoice_file(
            order_id=allegro_order_id,
            invoice_id=declaration["id"],
            pdf_bytes=pdf_bytes,
        )
    except Exception as exc:
        logger.exception(
            "Fetching or pushing Fakturownia invoice %s to Allegro failed for order %s",
            fakturownia_invoice_id,
            allegro_order_id,
        )
        _alert_invoice_failure(allegro_order_id=allegro_order_id, reason=str(exc))
        return {
            "status": "error",
            "error": str(exc),
            "fakturownia_invoice_id": fakturownia_invoice_id,
        }

    logger.info(
        "Created and pushed Fakturownia invoice %s (%s) for Allegro order %s",
        fakturownia_invoice_id,
        fakturownia_invoice_number,
        allegro_order_id,
    )
    return {
        "status": "created",
        "fakturownia_invoice_id": fakturownia_invoice_id,
        "fakturownia_invoice_number": fakturownia_invoice_number,
    }
