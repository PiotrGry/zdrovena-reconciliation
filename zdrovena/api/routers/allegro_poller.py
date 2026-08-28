"""zdrovena.api.routers.allegro_poller — poll Allegro for new orders.

Allegro has no webhooks, so we periodically poll ``GET /order/checkout-forms``
for orders in status ``READY_FOR_PROCESSING``. Each new order is mapped to a
Shopify-like payload and pushed through the shipping draft application service
so shipping logic (package calc, courier picking, phone/address normalisation)
is reused as-is.

Idempotency: a draft is created only if there is no existing original draft
with the same ``(source='allegro', external_order_id=<allegro id>)`` pair.
Errored drafts remain the retry target; they must never cause a second row.

Errors on one order do not block the others.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

from zdrovena.api.routers.allegro_invoicer import create_invoice_for_order
from zdrovena.api.shipping_draft_composition import (
    build_draft_record,
    emit_tracking_assigned,
    maybe_send_new_order_sms,
)
from zdrovena.common.allegro_mapper import allegro_to_shopify_order
from zdrovena.common.events import log_event
from zdrovena.shipping.application import drafts as draft_application

logger = logging.getLogger("zdrovena.api.routers.allegro_poller")

_MAX_AUTOMATIC_INVOICE_ATTEMPTS = 3


def _existing_allegro_draft(
    drafts: list[dict[str, Any]], external_order_id: str
) -> dict[str, Any] | None:
    errored_match: dict[str, Any] | None = None
    for d in drafts:
        if (
            d.get("source") == "allegro"
            and not d.get("is_replacement")
            and str(d.get("external_order_id", "")) == str(external_order_id)
        ):
            if d.get("status") != "error":
                return d
            if errored_match is None:
                errored_match = d
    return errored_match


def _invoice_attempt_count(draft: dict[str, Any]) -> int:
    try:
        return max(0, int(draft.get("fakturownia_invoice_attempts") or 0))
    except (TypeError, ValueError):
        return 0


def _needs_automatic_invoice_retry(draft: dict[str, Any]) -> bool:
    """Return whether an unfinished automatic invoice should be retried.

    A successful invoice has an id and no error. A partial failure may already
    have an id (for example when PDF upload failed); the invoicer resumes such
    documents idempotently by Allegro order ``oid``. Shipment creation is an
    independent lifecycle, so a ``created`` shipment must not suppress invoice
    recovery.
    """
    if draft.get("status") == "cancelled":
        return False
    invoice_id = draft.get("fakturownia_invoice_id")
    if invoice_id == "pending":
        return False
    if invoice_id and not draft.get("fakturownia_invoice_error"):
        return False
    return _invoice_attempt_count(draft) < _MAX_AUTOMATIC_INVOICE_ATTEMPTS


def _update_invoice_state(
    shipping_store: Any,
    *,
    draft_id: str,
    allegro_order_id: str,
    fields: dict[str, Any],
) -> bool:
    """Persist invoice state without letting storage failure stop the poller."""
    try:
        shipping_store.update_draft(draft_id, fields)
        return True
    except Exception:
        logger.exception(
            "Failed to persist automatic invoice state for Allegro order %s (draft %s)",
            allegro_order_id,
            draft_id,
        )
        return False


def poll_orders_once(
    *,
    client: Any,
    shipping_store: Any,
    storage: Any,
    fakturownia_client: Any = None,
    status: str = "READY_FOR_PROCESSING",
    fulfillment_status: str | None = "NEW",
    retry_existing_invoices: bool = True,
) -> dict[str, int]:
    """One polling cycle. Returns per-cycle stats.

    fakturownia_client is optional: pass it to also create + push a Fakturownia
    invoice for each newly-created draft (see allegro_invoicer.py). Omit it
    (or pass None) to skip invoicing entirely — e.g. in environments without
    Fakturownia credentials configured.

    retry_existing_invoices retries an unfinished invoice up to three times.
    Manual full-history sync disables it so it cannot backfill old orders by
    surprise; the scheduled NEW-order poll keeps it enabled.

    fulfillment_status defaults to "NEW" to skip already-shipped orders.
    Allegro's payment status (READY_FOR_PROCESSING) never changes after payment,
    so without a fulfillment filter all historical paid orders would be re-synced.
    """
    stats = {
        "fetched": 0,
        "created": 0,
        "updated": 0,
        "unchanged": 0,
        "skipped_duplicate": 0,
        "errors": 0,
        "invoices_created": 0,
        "invoice_errors": 0,
    }
    try:
        forms = client.list_orders(status=status, fulfillment_status=fulfillment_status)
    except Exception:
        # Resilience boundary: a poll cycle must never crash the scheduler, so we
        # catch broadly (network, auth, mapping) and surface it as an error stat.
        logger.exception("Allegro list_orders failed")
        stats["errors"] += 1
        return stats

    stats["fetched"] = len(forms)
    if not forms:
        return stats

    for form in forms:
        allegro_id = str(form.get("id", ""))
        if not allegro_id:
            logger.warning("Allegro checkout-form without id — skipping")
            stats["errors"] += 1
            continue

        try:
            # Targeted lookup per order. This used to build an index from
            # list_drafts(limit=10_000): sorted newest-first, it hid the oldest
            # rows once the store outgrew the limit, so the order was created
            # again — and the duplicate, written with created_at=now, evicted
            # another old row and duplicated on the next cycle (#316).
            existing = shipping_store.find_draft_by_external_id(
                source="allegro", external_order_id=allegro_id
            )
        except Exception:
            # Without a definite answer there is no idempotency decision.
            # Fail this order closed rather than risk a duplicate.
            logger.exception("Draft lookup failed for Allegro order %s", allegro_id)
            stats["errors"] += 1
            continue

        is_new = existing is None
        try:
            shopify_like = allegro_to_shopify_order(form)
            if existing:
                changed = draft_application.sync_draft_from_order(
                    shopify_like,
                    shipping_store,
                    build_draft_record=build_draft_record,
                    emit_tracking_assigned=emit_tracking_assigned,
                    record_event=log_event,
                    send_new_order_sms=maybe_send_new_order_sms,
                    source="allegro",
                    existing=existing,
                )
                if changed:
                    stats["updated"] += 1
                else:
                    stats["unchanged"] += 1
                stats["skipped_duplicate"] += 1
                draft = existing
            else:
                draft = draft_application.create_draft(
                    shopify_like,
                    shipping_store,
                    build_draft_record=build_draft_record,
                    emit_tracking_assigned=emit_tracking_assigned,
                    record_event=log_event,
                    send_new_order_sms=maybe_send_new_order_sms,
                    source="allegro",
                )
                stats["created"] += 1
        except Exception:
            # Resilience boundary: one malformed/failing order must not abort the
            # rest of the cycle. logger.exception captures the traceback (TRY400).
            logger.exception("Failed to sync draft for Allegro order %s", allegro_id)
            stats["errors"] += 1
            continue

        should_invoice = fakturownia_client is not None and (
            is_new
            or (
                retry_existing_invoices
                and existing is not None
                and _needs_automatic_invoice_retry(existing)
            )
        )
        if should_invoice:
            attempts = _invoice_attempt_count(draft) + 1
            invoice_state: dict[str, Any]
            try:
                invoice_result = create_invoice_for_order(
                    form, fakturownia_client=fakturownia_client, allegro_client=client
                )
                invoice_status = invoice_result.get("status")
                invoice_id = invoice_result.get("fakturownia_invoice_id") or draft.get(
                    "fakturownia_invoice_id"
                )
                invoice_number = invoice_result.get("fakturownia_invoice_number") or draft.get(
                    "fakturownia_invoice_number"
                )
                if invoice_status in {"created", "already_exists"} and invoice_id:
                    invoice_state = {
                        "fakturownia_invoice_id": invoice_id,
                        "fakturownia_invoice_number": invoice_number,
                        "fakturownia_invoice_error": None,
                    }
                else:
                    error = invoice_result.get("error") or (
                        f"Unexpected invoice result: {invoice_status}"
                    )
                    invoice_state = {
                        "fakturownia_invoice_id": invoice_id,
                        "fakturownia_invoice_number": invoice_number,
                        "fakturownia_invoice_error": error,
                    }
                    stats["invoice_errors"] += 1

                if invoice_status == "created":
                    stats["invoices_created"] += 1
            except Exception:
                # Resilience boundary: an invoicing failure must not block the
                # next order's draft — create_invoice_for_order already logs
                # and alerts internally, this only guards against a bug in
                # the orchestrator itself raising instead of returning "error".
                logger.exception("create_invoice_for_order raised for Allegro order %s", allegro_id)
                stats["invoice_errors"] += 1
                invoice_state = {
                    "fakturownia_invoice_id": draft.get("fakturownia_invoice_id"),
                    "fakturownia_invoice_number": draft.get("fakturownia_invoice_number"),
                    "fakturownia_invoice_error": "Unexpected automatic invoicing failure",
                }

            invoice_state.update(
                {
                    "fakturownia_invoice_attempts": attempts,
                    "fakturownia_invoice_attempted_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            if not _update_invoice_state(
                shipping_store,
                draft_id=draft["id"],
                allegro_order_id=allegro_id,
                fields=invoice_state,
            ):
                stats["errors"] += 1

        if not is_new:
            continue

        # Bezpieczny default: NIE oznaczamy zamówienia jako PROCESSING po samym utworzeniu draftu —
        # sam draft nie oznacza jeszcze nadania. Docelowo oznaczenie powinno paść w execute_draft po
        # sukcesie create-command. Za flagą ALLEGRO_MARK_ON_DRAFT=1 zachowujemy stare zachowanie.
        if os.getenv("ALLEGRO_MARK_ON_DRAFT", "").strip() in ("1", "true", "True"):
            try:
                client.mark_order_processed(allegro_id)
            except Exception as exc:
                logger.warning(
                    "Draft created but mark_order_processed failed for %s: %s",
                    allegro_id,
                    exc,
                )

    return stats
