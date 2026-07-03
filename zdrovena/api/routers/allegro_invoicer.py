"""zdrovena.api.routers.allegro_invoicer — create + push a Fakturownia
invoice for an Allegro order.

Replaces reliance on Fakturownia's Allegro app-store integration, which
does not compute kaucja (deposit) and which this business does not control.
Instead of patching an already-wrong invoice after the fact, this creates
the invoice correctly the first time (kaucja baked in via
settlement_positions from allegro_invoice_mapper) and pushes it to Allegro
via the order-invoices API.

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
from typing import Any

from zdrovena.common.allegro_invoice_mapper import allegro_order_to_fakturownia_invoice
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


def create_invoice_for_order(
    order: dict[str, Any],
    *,
    fakturownia_client: Any,
    allegro_client: Any,
) -> dict[str, Any]:
    """Create a Fakturownia invoice for one Allegro order and push it back.

    Returns a dict with at least a "status" key:
      "not_required"   — buyer did not request a VAT invoice, nothing to do
      "already_exists" — Fakturownia already has an invoice for this order
      "created"        — success; also has fakturownia_invoice_id/number
      "error"          — also has "error" (str); fakturownia_invoice_id is
                          present if Fakturownia succeeded but the Allegro
                          push failed
    """
    allegro_order_id = str(order.get("id") or "")

    payload = allegro_order_to_fakturownia_invoice(order)
    if payload is None:
        return {"status": "not_required"}

    try:
        existing = fakturownia_client.list_invoices(oid=allegro_order_id)
    except Exception as exc:
        logger.exception(
            "Fakturownia list_invoices lookup failed for Allegro order %s", allegro_order_id
        )
        _alert_invoice_failure(allegro_order_id=allegro_order_id, reason=str(exc))
        return {"status": "error", "error": str(exc)}

    if existing:
        logger.info(
            "Fakturownia already has an invoice for Allegro order %s — skipping", allegro_order_id
        )
        return {"status": "already_exists"}

    try:
        created = fakturownia_client.create_invoice(payload)
        fakturownia_invoice_id = created["id"]
        fakturownia_invoice_number = created["number"]
    except Exception as exc:
        logger.exception("Fakturownia create_invoice failed for Allegro order %s", allegro_order_id)
        _alert_invoice_failure(allegro_order_id=allegro_order_id, reason=str(exc))
        return {"status": "error", "error": str(exc)}

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
            "Pushing Fakturownia invoice %s to Allegro failed for order %s",
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
