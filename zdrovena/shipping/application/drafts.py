"""Draft creation and synchronization application services.

The functions in this module coordinate primitive draft dictionaries through
injected collaborators. They intentionally know nothing about FastAPI, provider
clients, or the concrete persistence backend.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from zdrovena.common.shipping_format import normalize_pl_phone


class DraftRepository(Protocol):
    """Minimal persistence boundary required by draft lifecycle operations."""

    def upsert_draft(self, record: dict[str, Any]) -> None: ...


BuildDraftRecord = Callable[..., dict[str, Any]]
EmitTrackingAssigned = Callable[[Any, Any, str], None]
RecordEvent = Callable[..., None]
SendNewOrderSms = Callable[[dict[str, Any]], None]


_SYNC_PRESERVED_FIELDS = {
    "id",
    "created_at",
    "execution_started_at",
    "shipment_origin",
    "courier_draft_id",
    "courier_shipments",
    "dispatch_order_id",
    "allegro_shipment_id",
    "allegro_dispatch_id",
    "allegro_pickup_command_id",
    "pickup_ordered",
    "fakturownia_invoice_id",
    "fakturownia_invoice_number",
    "fakturownia_invoice_error",
    "fakturownia_invoice_attempts",
    "fakturownia_invoice_attempted_at",
    "allegro_fulfillment_status",
}

_SYNC_TERMINAL_STATUSES = {"created", "cancelled"}
_SYNC_BUSY_STATUSES = {"executing", "pending_confirmation"}
_SYNC_EXECUTION_STARTED_STATUSES = {"executing", "pending_confirmation", "created"}
_MATCH_MANUAL = "manual"
_PACKAGES_SOURCE_OPERATOR = "operator"
_MATCH_FIELDS = (
    "shipping_service_match_status",
    "shipping_service_match_source",
    "shipping_service_match_detail",
)
_SHIPMENT_ORIGIN_SYSTEM = "system"
_SHIPMENT_ORIGIN_EXTERNAL = "external"


def source_fulfillment_status(order: dict[str, Any], *, source: str) -> str | None:
    raw = str(order.get("fulfillment_status") or "").strip().lower()
    if source == "allegro":
        if raw in {"sent", "picked_up"}:
            return "fulfilled"
        if raw in {"processing", "ready_for_shipment"}:
            return "processing"
        if raw:
            return raw
        return None
    if raw == "fulfilled":
        return "fulfilled"
    if raw == "partial":
        return "partial"
    if raw:
        return raw
    fulfillments = order.get("fulfillments") or []
    if fulfillments:
        return "fulfilled"
    return None


def source_cancelled(order: dict[str, Any]) -> bool:
    return bool(order.get("cancelled_at") or order.get("cancelled") is True)


def source_fulfillment_details(order: dict[str, Any]) -> dict[str, Any]:
    fulfillments = order.get("fulfillments") or []
    if not isinstance(fulfillments, list):
        return {}
    for fulfillment in fulfillments:
        if not isinstance(fulfillment, dict):
            continue
        tracking_number = fulfillment.get("tracking_number")
        tracking_numbers = fulfillment.get("tracking_numbers")
        if not tracking_number and isinstance(tracking_numbers, list) and tracking_numbers:
            tracking_number = tracking_numbers[0]
        if tracking_number:
            return {
                "tracking_number": tracking_number,
                "tracking_company": fulfillment.get("tracking_company"),
                "fulfilled_at": fulfillment.get("updated_at") or fulfillment.get("created_at"),
                "shopify_fulfillment_id": str(fulfillment.get("id", "")) or None,
            }
    return {}


def status_from_source(order: dict[str, Any], fallback: str, *, source: str) -> str:
    source_fulfillment = source_fulfillment_status(order, source=source)
    if source_cancelled(order):
        return "cancelled"
    if source_fulfillment == "cancelled":
        return "cancelled"
    if source_fulfillment == "fulfilled":
        return "created"
    return fallback


def _inpost_phone_missing(draft: dict[str, Any]) -> bool:
    """Return whether an InPost draft lacks a phone the carrier will accept.

    InPost enforces this from 2026-09-08. A draft in that state cannot ship, so
    it must not sit in ``pending`` looking ready.
    """
    if draft.get("courier") != "inpost":
        return False
    return not normalize_pl_phone((draft.get("receiver") or {}).get("phone"))


def merge_synced_draft(
    existing: dict[str, Any],
    incoming: dict[str, Any],
    *,
    emit_tracking_assigned: EmitTrackingAssigned,
) -> dict[str, Any]:
    merged = {**existing, **incoming}
    for field in _SYNC_PRESERVED_FIELDS:
        if field in existing:
            merged[field] = existing[field]
    if existing.get("fulfilled_at") and not incoming.get("fulfilled_at"):
        merged["fulfilled_at"] = existing["fulfilled_at"]

    # A tracking number we did not produce means the parcel was dispatched by
    # hand in a carrier portal. This event deliberately precedes persistence to
    # preserve the existing lifecycle ordering.
    if merged.get("tracking_number") and not merged.get("shipment_origin"):
        merged["shipment_origin"] = (
            _SHIPMENT_ORIGIN_SYSTEM if merged.get("courier_draft_id") else _SHIPMENT_ORIGIN_EXTERNAL
        )
        emit_tracking_assigned(
            merged.get("id"),
            merged.get("shopify_order_number"),
            merged["shipment_origin"],
        )

    existing_status = existing.get("status")
    incoming_status = incoming.get("status")

    if (
        existing_status == "pending_confirmation"
        and str(merged.get("tracking_number") or "").strip()
    ):
        merged["status"] = "created"
    elif existing_status in _SYNC_TERMINAL_STATUSES or existing_status in _SYNC_BUSY_STATUSES:
        merged["status"] = existing_status
    elif incoming_status == "created":
        merged["status"] = "created"
        if not merged.get("fulfilled_at"):
            merged["fulfilled_at"] = incoming.get("source_updated_at") or incoming.get("updated_at")
    elif incoming_status == "cancelled":
        merged["status"] = "cancelled"
    elif existing_status == "error":
        merged["status"] = "error"
        merged["error"] = existing.get("error")
    elif (
        existing_status == "pending"
        and incoming_status == "needs_review"
        and not merged.get("unreadable_products")
        and not _inpost_phone_missing(merged)
    ):
        # A draft the operator already cleared is not re-flagged. Two exceptions,
        # both executable states that cannot actually ship: a product name the
        # planner cannot read (the recomputed plan is a guess, and an executable
        # guess is how #1710-#1712 were packed wrong), and an InPost draft
        # without a phone the carrier will accept from 2026-09-08.
        merged["status"] = "pending"
    else:
        merged["status"] = incoming_status or existing_status

    if (
        existing.get("fulfillment_status") == "fulfilled"
        or incoming.get("fulfillment_status") == "fulfilled"
    ):
        merged["fulfillment_status"] = "fulfilled"

    if existing.get("apaczka_service_id") and incoming.get("courier") == existing.get("courier"):
        merged["apaczka_service_id"] = existing["apaczka_service_id"]
        if existing.get("shipping_service_match_status") == _MATCH_MANUAL:
            for field in _MATCH_FIELDS:
                if field in existing:
                    merged[field] = existing[field]
    if existing.get("packages_source") == _PACKAGES_SOURCE_OPERATOR and existing.get(
        "packages_breakdown"
    ):
        # Preserved conditionally, like a manual Apaczka service override: a
        # plan the operator corrected outranks a recomputed one, but a draft
        # nobody touched still re-plans when the order changes.
        merged["packages_breakdown"] = existing["packages_breakdown"]
        merged["packages_count"] = existing.get("packages_count") or sum(
            int(row.get("qty") or 0) for row in existing["packages_breakdown"]
        )
        merged["packages_source"] = _PACKAGES_SOURCE_OPERATOR
    if existing.get("service") and existing_status in _SYNC_BUSY_STATUSES | _SYNC_TERMINAL_STATUSES:
        merged["service"] = existing["service"]
        merged["courier"] = existing.get("courier", merged.get("courier"))
    if existing.get("execution_started_at") or existing_status in _SYNC_EXECUTION_STARTED_STATUSES:
        # The collection amount is part of the already-reviewed provider
        # contract. A later Shopify payment update must not rewrite the audit
        # record after shipment creation has started.
        merged["cod"] = existing.get("cod")
        merged["cod_error"] = existing.get("cod_error")
        # What divides that amount between parcels freezes with it. A resume
        # after a partial failure recomputes the split rather than reading a
        # stored one, so moving its inputs would hand the remaining parcels a
        # different share than the labels already at the carrier.
        if "shipping_price" in existing:
            merged["shipping_price"] = existing.get("shipping_price")
        if "order_items" in existing:
            merged["order_items"] = existing.get("order_items")
    if existing.get("tracking_number"):
        merged["tracking_number"] = existing["tracking_number"]
        merged["tracking_company"] = existing.get("tracking_company")

    return merged


def meaningful_draft_diff(before: dict[str, Any], after: dict[str, Any]) -> bool:
    ignored = {"updated_at"}
    keys = (set(before) | set(after)) - ignored
    return any(before.get(key) != after.get(key) for key in keys)


def persist_draft_from_order(
    order: dict[str, Any],
    repository: DraftRepository,
    *,
    build_draft_record: BuildDraftRecord,
    emit_tracking_assigned: EmitTrackingAssigned,
    record_event: RecordEvent,
    send_new_order_sms: SendNewOrderSms,
    source: str = "shopify",
    existing: dict[str, Any] | None = None,
) -> tuple[bool, dict[str, Any]]:
    record = build_draft_record(
        order,
        source=source,
        draft_id=existing.get("id") if existing else None,
        created_at=existing.get("created_at") if existing else None,
    )
    if existing is not None:
        record = merge_synced_draft(
            existing,
            record,
            emit_tracking_assigned=emit_tracking_assigned,
        )
    changed = existing is None or meaningful_draft_diff(existing, record)
    if changed:
        repository.upsert_draft(record)
    if existing is None:
        record_event(
            "draft.created",
            order_number=record["shopify_order_number"],
            draft_id=record["id"],
            source=source,
            courier=record["courier"],
            status=record["status"],
            packages_count=record["packages_count"],
        )
        send_new_order_sms(record)
    elif changed:
        record_event(
            "draft.updated_from_sync",
            order_number=record["shopify_order_number"],
            draft_id=record["id"],
            source=source,
            status=record["status"],
            fulfillment_status=record.get("fulfillment_status"),
        )
    return changed, record


def sync_draft_from_order(
    order: dict[str, Any],
    repository: DraftRepository,
    *,
    build_draft_record: BuildDraftRecord,
    emit_tracking_assigned: EmitTrackingAssigned,
    record_event: RecordEvent,
    send_new_order_sms: SendNewOrderSms,
    source: str = "shopify",
    existing: dict[str, Any] | None = None,
) -> bool:
    changed, _ = persist_draft_from_order(
        order,
        repository,
        build_draft_record=build_draft_record,
        emit_tracking_assigned=emit_tracking_assigned,
        record_event=record_event,
        send_new_order_sms=send_new_order_sms,
        source=source,
        existing=existing,
    )
    return changed


def create_draft(
    order: dict[str, Any],
    repository: DraftRepository,
    *,
    build_draft_record: BuildDraftRecord,
    emit_tracking_assigned: EmitTrackingAssigned,
    record_event: RecordEvent,
    send_new_order_sms: SendNewOrderSms,
    source: str = "shopify",
) -> dict[str, Any]:
    _, record = persist_draft_from_order(
        order,
        repository,
        build_draft_record=build_draft_record,
        emit_tracking_assigned=emit_tracking_assigned,
        record_event=record_event,
        send_new_order_sms=send_new_order_sms,
        source=source,
    )
    return record
