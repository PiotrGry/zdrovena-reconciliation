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
        "cod": {"amount": "200.30", "currency": "PLN"},
        "cod_error": None,
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
        "cod": None,
        "cod_error": "incoming payment state changed",
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
    assert merged["cod"] == {"amount": "200.30", "currency": "PLN"}
    assert merged["cod_error"] is None
    assert tracking_events == []


def test_cod_updates_from_shopify_until_execution_has_started() -> None:
    existing = {
        "id": "draft-cod-pending",
        "status": "pending",
        "courier": "apaczka",
        "service": "apaczka",
        "cod": {"amount": "200.30", "currency": "PLN"},
        "cod_error": None,
    }
    incoming = {
        **existing,
        "cod": {"amount": "180.00", "currency": "PLN"},
    }

    merged = merge_synced_draft(existing, incoming, emit_tracking_assigned=lambda *_: None)

    assert merged["cod"] == {"amount": "180.00", "currency": "PLN"}


def test_the_inputs_of_the_cod_split_freeze_with_the_amount() -> None:
    # The amount is frozen once execution starts because it is part of the
    # reviewed provider contract. What divides it between parcels has to freeze
    # with it: otherwise an order edited in Shopify would hand a resumed parcel
    # a different share than the label already at the carrier.
    existing = {
        "id": "draft-cod-executing",
        "status": "executing",
        "courier": "apaczka",
        "service": "apaczka",
        "cod": {"amount": "700.00", "currency": "PLN"},
        "cod_error": None,
        "shipping_price": "20.00",
        "order_items": [{"name": "HUMIO 42 butelki", "quantity": 1, "line_total": "680.00"}],
    }
    incoming = {
        **existing,
        "cod": {"amount": "500.00", "currency": "PLN"},
        "shipping_price": "0.00",
        "order_items": [{"name": "HUMIO 12 butelek", "quantity": 1, "line_total": "500.00"}],
    }

    merged = merge_synced_draft(existing, incoming, emit_tracking_assigned=lambda *_: None)

    assert merged["cod"] == {"amount": "700.00", "currency": "PLN"}
    assert merged["shipping_price"] == "20.00"
    assert merged["order_items"] == existing["order_items"]


def test_the_split_inputs_still_follow_shopify_before_execution_starts() -> None:
    existing = {
        "id": "draft-cod-pending",
        "status": "pending",
        "courier": "apaczka",
        "service": "apaczka",
        "cod": {"amount": "700.00", "currency": "PLN"},
        "cod_error": None,
        "shipping_price": "20.00",
        "order_items": [{"name": "HUMIO 42 butelki", "quantity": 1, "line_total": "680.00"}],
    }
    incoming = {
        **existing,
        "shipping_price": "0.00",
        "order_items": [{"name": "HUMIO 12 butelek", "quantity": 1, "line_total": "500.00"}],
    }

    merged = merge_synced_draft(existing, incoming, emit_tracking_assigned=lambda *_: None)

    assert merged["shipping_price"] == "0.00"
    assert merged["order_items"] == incoming["order_items"]


def test_cod_remains_immutable_after_a_failed_execution_attempt() -> None:
    existing = {
        "id": "draft-cod-started",
        "status": "error",
        "execution_started_at": "2026-08-17T10:00:00+00:00",
        "courier": "apaczka",
        "service": "apaczka",
        "cod": {"amount": "200.30", "currency": "PLN"},
        "cod_error": None,
    }
    incoming = {
        **existing,
        "execution_started_at": None,
        "cod": {"amount": "180.00", "currency": "PLN"},
        "cod_error": "incoming payment state changed",
    }

    merged = merge_synced_draft(existing, incoming, emit_tracking_assigned=lambda *_: None)

    assert merged["execution_started_at"] == "2026-08-17T10:00:00+00:00"
    assert merged["cod"] == {"amount": "200.30", "currency": "PLN"}
    assert merged["cod_error"] is None


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


def test_sync_keeps_a_draft_in_review_when_the_plan_is_a_guess() -> None:
    """A rename must not slip a guessed parcel plan past an operator.

    Sync normally downgrades an incoming `needs_review` back to `pending` so a
    draft the operator already cleared is not re-flagged. That downgrade cannot
    apply when the product name became unreadable: the recomputed plan is a
    guess, and an executable guess is how orders #1710-#1712 were packed wrong.
    """
    existing = {"id": "draft-1", "status": "pending", "unreadable_products": []}
    incoming = {
        "id": "new-id",
        "status": "needs_review",
        "unreadable_products": ["Kubek termiczny HUMIO"],
        "packages_breakdown": [{"type": "1-pak", "qty": 1}],
    }

    merged = merge_synced_draft(existing, incoming, emit_tracking_assigned=lambda *args: None)

    assert merged["status"] == "needs_review"
    assert merged["unreadable_products"] == ["Kubek termiczny HUMIO"]


def test_sync_still_respects_an_operator_who_cleared_a_readable_draft() -> None:
    """The existing downgrade survives when the plan is not a guess."""
    existing = {"id": "draft-1", "status": "pending"}
    incoming = {"id": "new-id", "status": "needs_review", "unreadable_products": []}

    merged = merge_synced_draft(existing, incoming, emit_tracking_assigned=lambda *args: None)

    assert merged["status"] == "pending"


class TestOperatorParcelPlanSurvivesSync:
    def test_keeps_the_operator_plan_and_its_count(self):
        # The planner recomputes on every sync. Without this, the operator's
        # correction is silently reverted and the wrong boxes ship — which is
        # exactly how #1710-#1712 went out.
        existing = {
            "id": "d1",
            "status": "pending",
            "packages_source": "operator",
            "packages_breakdown": [{"type": "szkło", "qty": 2}],
            "packages_count": 2,
        }
        incoming = {
            "id": "d1",
            "status": "pending",
            "packages_source": "planner",
            "packages_breakdown": [{"type": "1-pak", "qty": 1}],
            "packages_count": 1,
        }

        merged = merge_synced_draft(existing, incoming, emit_tracking_assigned=lambda *_: None)

        assert merged["packages_breakdown"] == [{"type": "szkło", "qty": 2}]
        assert merged["packages_count"] == 2
        assert merged["packages_source"] == "operator"

    def test_a_planner_plan_is_still_replaced_by_a_fresh_one(self):
        existing = {
            "id": "d1",
            "status": "pending",
            "packages_source": "planner",
            "packages_breakdown": [{"type": "1-pak", "qty": 1}],
            "packages_count": 1,
        }
        incoming = {
            "id": "d1",
            "status": "pending",
            "packages_source": "planner",
            "packages_breakdown": [{"type": "3-pak", "qty": 1}],
            "packages_count": 1,
        }

        merged = merge_synced_draft(existing, incoming, emit_tracking_assigned=lambda *_: None)

        assert merged["packages_breakdown"] == [{"type": "3-pak", "qty": 1}]

    def test_a_draft_written_before_the_field_existed_is_treated_as_planner(self):
        existing = {
            "id": "d1",
            "status": "pending",
            "packages_breakdown": [{"type": "1-pak", "qty": 1}],
            "packages_count": 1,
        }
        incoming = {
            "id": "d1",
            "status": "pending",
            "packages_source": "planner",
            "packages_breakdown": [{"type": "3-pak", "qty": 1}],
            "packages_count": 1,
        }

        merged = merge_synced_draft(existing, incoming, emit_tracking_assigned=lambda *_: None)

        assert merged["packages_breakdown"] == [{"type": "3-pak", "qty": 1}]

    def test_an_operator_draft_missing_its_plan_does_not_crash_the_sync(self):
        # Defensive: a KeyError here would break syncing for every draft, not
        # just this one. A malformed record must degrade, not take down the run.
        existing = {"id": "d1", "status": "pending", "packages_source": "operator"}
        incoming = {
            "id": "d1",
            "status": "pending",
            "packages_source": "planner",
            "packages_breakdown": [{"type": "3-pak", "qty": 1}],
            "packages_count": 1,
        }

        merged = merge_synced_draft(existing, incoming, emit_tracking_assigned=lambda *_: None)

        assert merged["packages_breakdown"] == [{"type": "3-pak", "qty": 1}]


class TestInPostPhoneReFlagsOnSync:
    """Issue #294. Without this, one "reviewed" click makes a phone-less InPost
    draft executable forever: merge_synced_draft deliberately keeps a cleared
    draft at pending, so every later sync preserved the broken state."""

    @staticmethod
    def _pair(existing_phone, incoming_phone, courier="inpost"):
        base = {"id": "d1", "courier": courier, "service": "inpost_courier_standard"}
        existing = {**base, "status": "pending", "receiver": {"phone": existing_phone}}
        incoming = {**base, "status": "needs_review", "receiver": {"phone": incoming_phone}}
        return existing, incoming

    def test_a_cleared_inpost_draft_without_a_phone_is_re_flagged(self):
        existing, incoming = self._pair(None, None)

        merged = merge_synced_draft(existing, incoming, emit_tracking_assigned=lambda *_: None)

        assert merged["status"] == "needs_review"

    def test_a_malformed_phone_counts_as_missing(self):
        existing, incoming = self._pair("12345", "12345")

        merged = merge_synced_draft(existing, incoming, emit_tracking_assigned=lambda *_: None)

        assert merged["status"] == "needs_review"

    def test_a_cleared_inpost_draft_with_a_phone_stays_cleared(self):
        existing, incoming = self._pair("+48600100200", "+48600100200")

        merged = merge_synced_draft(existing, incoming, emit_tracking_assigned=lambda *_: None)

        assert merged["status"] == "pending"

    def test_other_carriers_keep_the_old_behaviour(self):
        existing, incoming = self._pair(None, None, courier="apaczka")

        merged = merge_synced_draft(existing, incoming, emit_tracking_assigned=lambda *_: None)

        assert merged["status"] == "pending"
