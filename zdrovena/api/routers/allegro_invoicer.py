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

The scheduled caller retries unfinished invoices at most three times and stores
the attempt count on the shipping draft. This function remains idempotent: it
finds the Fakturownia document by the unique Allegro order ``oid``, avoids a
duplicate kaucja row, and reuses an existing Allegro invoice declaration.

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


def _finish_invoice(
    *,
    allegro_order_id: str,
    invoice_id: Any,
    invoice_number: Any,
    settlement_positions: list[dict[str, Any]],
    fakturownia_client: Any,
    allegro_client: Any,
    resuming: bool = False,
) -> dict[str, Any]:
    """Idempotently complete the post-create steps for an invoice.

    Steps, in order: attach any settlement positions (kaucja), mark the final
    payable amount as paid, fetch the PDF, and push it to Allegro. Safe to call
    on a freshly created invoice OR when recovering an existing one (the
    502-loop fix — R4.1):

      * ``add_settlement_position`` is a no-op when the row already exists
        (it re-reads the invoice and matches on ``reason``), so re-running it
        cannot duplicate the kaucja.
      * When ``resuming`` (recovery path), an existing Allegro declaration is
        matched by invoice number (``list_order_invoices``). If found, the PDF is
        re-uploaded to THAT declaration id (idempotent ``PUT``) rather than
        creating a second one — because Allegro's list does not report whether
        the file was actually uploaded, so re-upload is the safe default that
        guarantees the file is attached. On a fresh create the order has no
        declaration yet, so we create + upload directly.

    On any failure returns an ``error`` dict that PRESERVES
    ``fakturownia_invoice_id`` so the next retry resumes at the first
    still-incomplete step instead of orphaning the document.
    """
    try:
        for settlement in settlement_positions:
            fakturownia_client.add_settlement_position(
                invoice_id=invoice_id,
                kind=settlement["kind"],
                amount_pln=settlement["amount"],
                description=settlement["description"],
            )
    except Exception as exc:
        logger.exception(
            "Adding settlement position to Fakturownia invoice %s failed for order %s",
            invoice_id,
            allegro_order_id,
        )
        _alert_invoice_failure(allegro_order_id=allegro_order_id, reason=str(exc))
        return {"status": "error", "error": str(exc), "fakturownia_invoice_id": invoice_id}

    try:
        # Allegro orders reach this flow only after payment.  Kaucja is added
        # after invoice creation, so the paid status must be applied AFTER all
        # settlement positions; otherwise Fakturownia keeps only the base
        # positions paid and reports the deposit as outstanding.
        fakturownia_client.change_invoice_status(invoice_id, "paid")
    except Exception as exc:
        logger.exception(
            "Marking Fakturownia invoice %s fully paid failed for Allegro order %s",
            invoice_id,
            allegro_order_id,
        )
        _alert_invoice_failure(allegro_order_id=allegro_order_id, reason=str(exc))
        return {"status": "error", "error": str(exc), "fakturownia_invoice_id": invoice_id}

    try:
        existing_declaration_id: str | None = None
        if resuming:
            # Match the declaration for THIS invoice by number — not "any invoice
            # exists" (that was too coarse: a declaration for a different invoice,
            # or one whose PDF upload had failed, would wrongly count as done).
            for inv in allegro_client.list_order_invoices(allegro_order_id) or []:
                if str(inv.get("invoiceNumber") or "") == str(invoice_number):
                    existing_declaration_id = inv.get("id")
                    break

        if existing_declaration_id is not None:
            # A declaration for this invoice already exists. LIMITATION: Allegro's
            # invoice list returns invoiceNumber/fileType but NOT whether the PDF
            # bytes were actually uploaded, so we cannot distinguish "declaration
            # created, upload failed" from "fully done". Safest strategy: re-upload
            # the PDF to the EXISTING declaration id. `PUT …/invoices/{id}/file` is
            # idempotent (replaces the file), so this cannot duplicate the
            # declaration or the invoice — it only guarantees the file is attached.
            # We deliberately do NOT create a second declaration.
            logger.info(
                "Allegro order %s already has a declaration for invoice %s — re-uploading PDF "
                "to existing declaration %s (Allegro does not expose file-attachment status)",
                allegro_order_id,
                invoice_number,
                existing_declaration_id,
            )
            pdf_bytes = fakturownia_client.get_invoice_pdf(invoice_id)
            allegro_client.upload_invoice_file(
                order_id=allegro_order_id,
                invoice_id=existing_declaration_id,
                pdf_bytes=pdf_bytes,
            )
        else:
            pdf_bytes = fakturownia_client.get_invoice_pdf(invoice_id)
            declaration = allegro_client.create_invoice_declaration(
                order_id=allegro_order_id, invoice_number=invoice_number
            )
            allegro_client.upload_invoice_file(
                order_id=allegro_order_id,
                invoice_id=declaration["id"],
                pdf_bytes=pdf_bytes,
            )
    except Exception as exc:
        logger.exception(
            "Fetching or pushing Fakturownia invoice %s to Allegro failed for order %s",
            invoice_id,
            allegro_order_id,
        )
        _alert_invoice_failure(allegro_order_id=allegro_order_id, reason=str(exc))
        return {"status": "error", "error": str(exc), "fakturownia_invoice_id": invoice_id}

    return {
        "status": "created",
        "fakturownia_invoice_id": invoice_id,
        "fakturownia_invoice_number": invoice_number,
    }


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

    settlement_positions = payload.get("settlement_positions") or []

    if existing:
        # Recover the existing invoice (Fakturownia is the source of truth;
        # oid+oid_unique guarantees at most one match, so existing[0] is
        # canonical) and RESUME the incomplete steps rather than skipping them.
        #
        # The 502-loop bug had two halves: the first fix stopped resetting local
        # state to None (so retries no longer looped). But simply returning
        # "already_exists" here left the real gap — if the invoice was created
        # yet the settlement/PDF/Allegro-push failed, it was never finished, so
        # the order silently lacked its invoice on Allegro. _finish_invoice is
        # idempotent, so re-running it completes only the missing steps and
        # never duplicates the kaucja or the Allegro declaration.
        existing_invoice = existing[0]
        invoice_id = existing_invoice.get("id")
        invoice_number = existing_invoice.get("number")
        logger.info(
            "Fakturownia already has an invoice for Allegro order %s (id=%s) — "
            "recovering and resuming any incomplete steps",
            allegro_order_id,
            invoice_id,
        )
        result = _finish_invoice(
            allegro_order_id=allegro_order_id,
            invoice_id=invoice_id,
            invoice_number=invoice_number,
            settlement_positions=settlement_positions,
            fakturownia_client=fakturownia_client,
            allegro_client=allegro_client,
            resuming=True,
        )
        # No NEW Fakturownia invoice was created — keep the "already_exists"
        # status the caller/endpoint contract expects. Errors keep their status
        # (and the recovered id) so the next retry resumes again.
        if result["status"] == "created":
            result["status"] = "already_exists"
        return result

    _check_total_matches_allegro(order, payload)

    invoice_body = {k: v for k, v in payload.items() if k != "settlement_positions"}

    try:
        created = fakturownia_client.create_invoice(invoice_body)
        fakturownia_invoice_id = created["id"]
        fakturownia_invoice_number = created["number"]
    except Exception as exc:
        logger.exception("Fakturownia create_invoice failed for Allegro order %s", allegro_order_id)
        _alert_invoice_failure(allegro_order_id=allegro_order_id, reason=str(exc))
        return {"status": "error", "error": str(exc)}

    result = _finish_invoice(
        allegro_order_id=allegro_order_id,
        invoice_id=fakturownia_invoice_id,
        invoice_number=fakturownia_invoice_number,
        settlement_positions=settlement_positions,
        fakturownia_client=fakturownia_client,
        allegro_client=allegro_client,
    )
    if result["status"] == "created":
        logger.info(
            "Created and pushed Fakturownia invoice %s (%s) for Allegro order %s",
            fakturownia_invoice_id,
            fakturownia_invoice_number,
            allegro_order_id,
        )
    return result
