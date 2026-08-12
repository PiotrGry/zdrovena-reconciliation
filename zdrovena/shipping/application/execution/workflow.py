"""HTTP-neutral shipment execution orchestration."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from zdrovena.common.shipping_exceptions import ZdrovenaShippingError
from zdrovena.common.shipping_state import EXECUTING
from zdrovena.shipping.application.execution.fingerprint import fingerprints_match


class ExecutionRepository(Protocol):
    """Persistence capabilities required by shipment execution."""

    def get_draft(self, draft_id: str) -> dict[str, Any] | None: ...

    def try_claim_execution(self, draft_id: str) -> bool: ...

    def update_draft(self, draft_id: str, fields: dict[str, Any]) -> bool: ...

    def enqueue_dlq(
        self,
        *,
        payload: dict[str, Any],
        error: str,
        source: str,
        kind: str,
        draft_id: str,
        entry_id: str | None,
    ) -> dict[str, Any]: ...


PreviewBuilder = Callable[[dict[str, Any]], dict[str, Any]]
SenderResolver = Callable[[], dict[str, str]]
ProviderRunner = Callable[..., dict[str, Any]]
RecordEvent = Callable[..., None]
EmitTrackingAssigned = Callable[[Any, Any, str], None]
PushTracking = Callable[[dict[str, Any]], None]
LogException = Callable[..., None]


@dataclass(frozen=True)
class PickupWindow:
    """Optional pickup schedule supplied with one execution command."""

    date: str | None = None
    from_time: str | None = None
    to_time: str | None = None


@dataclass(frozen=True)
class ProviderRunners:
    """Concrete provider runners selected by the execution workflow."""

    inpost: ProviderRunner
    apaczka: ProviderRunner
    allegro_delivery: ProviderRunner


@dataclass(frozen=True)
class ExecutionEffects:
    """Observable effects emitted after execution and during failure handling."""

    record_event: RecordEvent
    emit_tracking_assigned: EmitTrackingAssigned
    push_tracking: PushTracking
    log_exception: LogException


class DraftNotFoundError(LookupError):
    """The requested draft does not exist."""

    def __init__(self) -> None:
        super().__init__("Draft not found")


class DraftRequiresReviewError(RuntimeError):
    """The draft must be reviewed before execution."""

    def __init__(self) -> None:
        super().__init__("Draft requires review (multi-package) — use PATCH to override")


class PreviewFingerprintMismatchError(RuntimeError):
    """The persisted draft no longer matches the reviewed preview."""

    def __init__(self) -> None:
        super().__init__("Draft changed after preview — review the courier payload again.")


class ExecutionClaimConflictError(RuntimeError):
    """Another actor won the claim or the draft is no longer executable."""

    def __init__(self) -> None:
        super().__init__("Draft already executed or in progress — nie realizuj ponownie.")


class ExecutionCommunicationError(RuntimeError):
    """A non-shipping exception occurred after the execution claim."""

    def __init__(self, original: Exception) -> None:
        self.original = original
        super().__init__(str(original))


def record_execution_failure(
    repository: ExecutionRepository,
    draft: dict[str, Any],
    error: str,
    *,
    execution_dlq_kind: str,
    record_event: RecordEvent,
    log_exception: LogException,
    entry_id: str | None = None,
) -> None:
    """Persist execution failure bookkeeping without masking the real error."""
    draft_id = str(draft.get("id") or "")
    try:
        entry = repository.enqueue_dlq(
            payload=draft,
            error=error,
            source=str(draft.get("source") or "shopify"),
            kind=execution_dlq_kind,
            draft_id=draft_id,
            entry_id=entry_id,
        )
        record_event(
            "dlq.enqueued",
            level=logging.ERROR,
            entry_id=entry["id"],
            order_number=draft.get("shopify_order_number") or draft.get("order_number"),
            source=entry["source"],
            error_type=error.split(":", 1)[0],
            kind=execution_dlq_kind,
            draft_id=draft_id,
        )
    except Exception:
        log_exception("Failed to enqueue execution failure for draft %s", draft_id)


def release_execution_claim(
    repository: ExecutionRepository,
    draft_id: str,
    error: str,
    *,
    log_exception: LogException,
) -> None:
    """Return a still-executing draft to error without clobbering later states."""
    try:
        current = repository.get_draft(draft_id)
        if current and current.get("status") == EXECUTING:
            repository.update_draft(draft_id, {"status": "error", "error": error})
    except Exception:
        log_exception("Failed to release execution claim for draft %s (left as-is)", draft_id)


def execute_draft(
    draft_id: str,
    repository: ExecutionRepository,
    *,
    build_preview: PreviewBuilder,
    resolve_sender: SenderResolver,
    providers: ProviderRunners,
    effects: ExecutionEffects,
    execution_dlq_kind: str,
    system_shipment_origin: str,
    pickup_window: PickupWindow | None = None,
    preview_fingerprint: str | None = None,
    failure_dlq_entry_id: str | None = None,
) -> dict[str, Any]:
    """Execute one draft while coordinating claims, checkpoints, and side effects."""
    draft = repository.get_draft(draft_id)
    if not draft:
        raise DraftNotFoundError
    if draft.get("status") == "needs_review":
        raise DraftRequiresReviewError

    reviewed_preview: dict[str, Any] | None = None
    if preview_fingerprint is not None:
        reviewed_preview = build_preview(draft)
        if not fingerprints_match(reviewed_preview["fingerprint"], preview_fingerprint):
            raise PreviewFingerprintMismatchError

    if not repository.try_claim_execution(draft_id):
        raise ExecutionClaimConflictError

    try:
        effective_pickup_window = pickup_window or PickupWindow()
        pickup_schedule = {
            "pickup_date": effective_pickup_window.date,
            "pickup_from": effective_pickup_window.from_time,
            "pickup_to": effective_pickup_window.to_time,
        }
        sender = (
            reviewed_preview["sender"]
            if reviewed_preview is not None and draft.get("courier") == "inpost"
            else resolve_sender()
        )
        persisted_shipments = list(draft.get("courier_shipments") or [])

        def persist_shipment(shipment: dict[str, str]) -> None:
            persisted_shipments.append(shipment)
            repository.update_draft(draft_id, {"courier_shipments": persisted_shipments})

        def persist_allegro_command(command_id: str) -> None:
            repository.update_draft(
                draft_id,
                {
                    "allegro_command_id": command_id,
                    "status": "pending_confirmation",
                    "error": None,
                },
            )

        def persist_allegro_pickup_command(command_id: str) -> None:
            repository.update_draft(
                draft_id,
                {
                    "allegro_pickup_command_id": command_id,
                    "allegro_dispatch_id": None,
                },
            )

        def persist_allegro_shipment(shipment: dict[str, str]) -> None:
            key = (shipment.get("package_type"), shipment.get("package_number"))
            for index, existing in enumerate(persisted_shipments):
                if (existing.get("package_type"), existing.get("package_number")) == key:
                    persisted_shipments[index] = shipment
                    break
            else:
                persisted_shipments.append(shipment)
            repository.update_draft(draft_id, {"courier_shipments": persisted_shipments})

        courier = draft.get("courier", "apaczka")
        if courier == "allegro_delivery":
            patch = providers.allegro_delivery(
                draft,
                **pickup_schedule,
                on_command_created=persist_allegro_command,
                on_pickup_command_created=persist_allegro_pickup_command,
                on_shipment_checkpoint=persist_allegro_shipment,
            )
        elif courier == "inpost":
            patch = providers.inpost(
                draft,
                sender,
                **pickup_schedule,
                on_shipment_created=persist_shipment,
            )
        else:
            patch = providers.apaczka(
                draft,
                **pickup_schedule,
                on_shipment_created=persist_shipment,
            )

        if patch.get("courier_draft_id"):
            patch["shipment_origin"] = system_shipment_origin

        repository.update_draft(draft_id, patch)
        updated = repository.get_draft(draft_id)
        effects.record_event(
            "shipment.created",
            draft_id=draft_id,
            order_number=draft.get("shopify_order_number"),
            courier=draft.get("courier"),
            tracking_number=patch.get("tracking_number"),
            status=patch.get("status"),
        )
        if patch.get("tracking_number"):
            effects.emit_tracking_assigned(
                draft_id,
                draft.get("shopify_order_number"),
                patch.get("shipment_origin") or system_shipment_origin,
            )
        if updated:
            effects.push_tracking(updated)
        return updated or patch
    except ZdrovenaShippingError as exc:
        effects.log_exception("execute_draft failed for %s: %s", draft_id, exc)
        record_execution_failure(
            repository,
            draft,
            f"{type(exc).__name__}: {exc}",
            execution_dlq_kind=execution_dlq_kind,
            record_event=effects.record_event,
            log_exception=effects.log_exception,
            entry_id=failure_dlq_entry_id,
        )
        release_execution_claim(
            repository,
            draft_id,
            str(exc),
            log_exception=effects.log_exception,
        )
        raise
    except Exception as exc:
        effects.log_exception("execute_draft failed for %s: %s", draft_id, exc)
        record_execution_failure(
            repository,
            draft,
            f"{type(exc).__name__}: {exc}",
            execution_dlq_kind=execution_dlq_kind,
            record_event=effects.record_event,
            log_exception=effects.log_exception,
            entry_id=failure_dlq_entry_id,
        )
        release_execution_claim(
            repository,
            draft_id,
            str(exc),
            log_exception=effects.log_exception,
        )
        raise ExecutionCommunicationError(exc) from exc
