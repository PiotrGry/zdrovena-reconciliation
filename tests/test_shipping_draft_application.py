"""Characterization tests for HTTP-neutral shipping draft lifecycle orchestration."""

from __future__ import annotations

from typing import Any

import pytest

from zdrovena.shipping.application.drafts import (
    create_draft,
    meaningful_draft_diff,
    merge_synced_draft,
    persist_draft_from_order,
    source_fulfillment_details,
    source_fulfillment_status,
    status_from_source,
    sync_draft_from_order,
)


class RecordingDraftRepository:
    def __init__(
        self,
        calls: list[tuple[str, Any]],
        *,
        upsert_error: Exception | None = None,
    ) -> None:
        self.calls = calls
        self.records: list[dict[str, Any]] = []
        self.upsert_error = upsert_error

    def upsert_draft(self, record: dict[str, Any]) -> None:
        if self.upsert_error is not None:
            raise self.upsert_error
        self.calls.append(("upsert", record["id"]))
        self.records.append(record)


def _incoming_record(
    order: dict[str, Any],
    *,
    source: str = "shopify",
    draft_id: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    return {
        "id": draft_id or "new-draft",
        "created_at": created_at or "new-created-at",
        "updated_at": "incoming-updated-at",
        "shopify_order_number": str(order.get("order_number") or "1001"),
        "courier": "inpost",
        "status": "pending",
        "packages_count": 1,
        "source": source,
        "tracking_number": order.get("tracking_number"),
        "courier_draft_id": order.get("courier_draft_id"),
        "fulfillment_status": order.get("fulfillment_status"),
    }


def _callbacks(calls: list[tuple[str, Any]]) -> dict[str, Any]:
    def emit_tracking(draft_id: Any, order_number: Any, origin: str) -> None:
        calls.append(("tracking", (draft_id, order_number, origin)))

    def record_event(name: str, **fields: Any) -> None:
        calls.append(("event", (name, fields)))

    def notify(record: dict[str, Any]) -> None:
        calls.append(("sms", record["id"]))

    return {
        "build_draft_record": _incoming_record,
        "emit_tracking_assigned": emit_tracking,
        "record_event": record_event,
        "send_new_order_sms": notify,
    }


def test_create_draft_persists_then_emits_event_then_sends_sms() -> None:
    calls: list[tuple[str, Any]] = []
    repository = RecordingDraftRepository(calls)

    record = create_draft(
        {"order_number": "1001"},
        repository,
        **_callbacks(calls),
    )

    assert repository.records == [record]
    assert [name for name, _ in calls] == ["upsert", "event", "sms"]
    assert calls[1][1][0] == "draft.created"


def test_sync_tracking_event_precedes_persistence() -> None:
    calls: list[tuple[str, Any]] = []
    repository = RecordingDraftRepository(calls)
    existing = {
        "id": "draft-1",
        "created_at": "original-created-at",
        "updated_at": "old-updated-at",
        "shopify_order_number": "1001",
        "courier": "inpost",
        "status": "pending_confirmation",
        "packages_count": 1,
        "source": "shopify",
        "tracking_number": None,
        "courier_draft_id": None,
        "fulfillment_status": None,
    }

    changed, record = persist_draft_from_order(
        {"order_number": "1001", "tracking_number": "TRACK-1"},
        repository,
        existing=existing,
        **_callbacks(calls),
    )

    assert changed is True
    assert record["shipment_origin"] == "external"
    assert record["status"] == "created"
    assert [name for name, _ in calls] == ["tracking", "upsert", "event"]
    assert calls[0][1] == ("draft-1", "1001", "external")
    assert calls[2][1][0] == "draft.updated_from_sync"


def test_sync_passes_existing_identity_to_record_builder() -> None:
    calls: list[tuple[str, Any]] = []
    repository = RecordingDraftRepository(calls)
    received: dict[str, Any] = {}

    def build_record(order: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        received.update(kwargs)
        return _incoming_record(order, **kwargs)

    collaborators = _callbacks(calls)
    collaborators["build_draft_record"] = build_record
    existing = _incoming_record(
        {"order_number": "1001"},
        draft_id="existing-id",
        created_at="existing-created-at",
    )

    sync_draft_from_order(
        {"order_number": "1001"},
        repository,
        existing=existing,
        **collaborators,
    )

    assert received == {
        "source": "shopify",
        "draft_id": "existing-id",
        "created_at": "existing-created-at",
    }


def test_updated_at_only_difference_is_not_persisted_or_announced() -> None:
    calls: list[tuple[str, Any]] = []
    repository = RecordingDraftRepository(calls)
    existing = _incoming_record({"order_number": "1001"})
    existing["updated_at"] = "old-updated-at"

    changed = sync_draft_from_order(
        {"order_number": "1001"},
        repository,
        existing=existing,
        **_callbacks(calls),
    )

    assert changed is False
    assert calls == []
    assert meaningful_draft_diff(existing, {**existing, "updated_at": "different"}) is False


def test_merge_preserves_manual_service_match_and_terminal_execution_fields() -> None:
    tracking_events: list[tuple[Any, Any, str]] = []
    existing = {
        "id": "draft-1",
        "created_at": "created-at",
        "status": "created",
        "courier": "apaczka",
        "service": "apaczka",
        "apaczka_service_id": "23",
        "shipping_service_match_status": "manual",
        "shipping_service_match_source": "operator",
        "shipping_service_match_detail": "selected manually",
        "tracking_number": "TRACK-1",
        "tracking_company": "DPD",
        "courier_draft_id": "provider-1",
        "shipment_origin": "system",
    }
    incoming = {
        "id": "new-id",
        "created_at": "new-created-at",
        "status": "needs_review",
        "courier": "apaczka",
        "service": "inpost_locker_standard",
        "apaczka_service_id": None,
        "shipping_service_match_status": "requires_selection",
        "shipping_service_match_source": "source title",
        "shipping_service_match_detail": "new mapping",
        "tracking_number": None,
    }

    merged = merge_synced_draft(
        existing,
        incoming,
        emit_tracking_assigned=lambda *args: tracking_events.append(args),
    )

    assert merged["id"] == "draft-1"
    assert merged["created_at"] == "created-at"
    assert merged["status"] == "created"
    assert merged["courier"] == "apaczka"
    assert merged["service"] == "apaczka"
    assert merged["apaczka_service_id"] == "23"
    assert merged["shipping_service_match_status"] == "manual"
    assert merged["shipping_service_match_source"] == "operator"
    assert merged["shipping_service_match_detail"] == "selected manually"
    assert merged["tracking_number"] == "TRACK-1"
    assert tracking_events == []


def test_source_status_mapping_preserves_shopify_and_allegro_rules() -> None:
    assert source_fulfillment_status({"fulfillment_status": "fulfilled"}, source="shopify") == (
        "fulfilled"
    )
    assert source_fulfillment_status({"fulfillments": [{"id": 1}]}, source="shopify") == (
        "fulfilled"
    )
    assert source_fulfillment_status({"fulfillment_status": "PICKED_UP"}, source="allegro") == (
        "fulfilled"
    )
    assert status_from_source({"cancelled": True}, "pending", source="allegro") == "cancelled"
    assert status_from_source({"fulfillment_status": "SENT"}, "pending", source="allegro") == (
        "created"
    )


def test_source_fulfillment_details_uses_first_tracking_fulfillment() -> None:
    details = source_fulfillment_details(
        {
            "fulfillments": [
                {"id": "empty"},
                {
                    "id": 17,
                    "tracking_numbers": ["TRACK-17", "TRACK-18"],
                    "tracking_company": "InPost",
                    "created_at": "created-at",
                },
            ]
        }
    )

    assert details == {
        "tracking_number": "TRACK-17",
        "tracking_company": "InPost",
        "fulfilled_at": "created-at",
        "shopify_fulfillment_id": "17",
    }


def test_create_draft_does_not_swallow_builder_or_repository_errors() -> None:
    calls: list[tuple[str, Any]] = []
    repository = RecordingDraftRepository(calls)
    collaborators = _callbacks(calls)

    def broken_builder(order: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("builder failed")

    collaborators["build_draft_record"] = broken_builder
    with pytest.raises(RuntimeError, match="builder failed"):
        create_draft({"order_number": "1001"}, repository, **collaborators)

    collaborators["build_draft_record"] = _incoming_record
    repository = RecordingDraftRepository(calls, upsert_error=RuntimeError("store failed"))
    with pytest.raises(RuntimeError, match="store failed"):
        create_draft({"order_number": "1001"}, repository, **collaborators)
