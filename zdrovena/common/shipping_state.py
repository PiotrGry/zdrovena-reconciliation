"""zdrovena.common.shipping_state — shipping-draft lifecycle state machine.

R5-A: the allowed draft states and the transitions between them, defined in one
place so ``execute_draft`` / claim / fulfillment logic can validate moves
deterministically instead of scattering ad-hoc ``status == "..."`` checks.

Lifecycle:

    pending ─────────────┐
    pending_confirmation ┤→ executing → created ──→ (fulfilled via a separate
    error ───────────────┘        │                 fulfillment_status flag)
                                   └────→ error → (retry) executing
    needs_review → pending
    any active → cancelled

``executing`` is the atomic *claim* state (R5-A): a draft is moved into it under
optimistic concurrency (ETag) before the courier call, so two concurrent
execute requests cannot both proceed and create duplicate shipments.
"""

from __future__ import annotations

PENDING = "pending"
NEEDS_REVIEW = "needs_review"
PENDING_CONFIRMATION = "pending_confirmation"
EXECUTING = "executing"
CREATED = "created"
ERROR = "error"
CANCELLED = "cancelled"

#: States from which a fresh execution claim may be taken.
EXECUTABLE_STATES: frozenset[str] = frozenset({PENDING, PENDING_CONFIRMATION, ERROR})

#: Allowed status transitions. A move not listed here is rejected.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    PENDING: frozenset({EXECUTING, NEEDS_REVIEW, CANCELLED}),
    NEEDS_REVIEW: frozenset({PENDING, CANCELLED}),
    PENDING_CONFIRMATION: frozenset({EXECUTING, CANCELLED}),
    EXECUTING: frozenset({CREATED, ERROR}),
    CREATED: frozenset({CANCELLED}),
    ERROR: frozenset({EXECUTING, CANCELLED}),
    CANCELLED: frozenset(),
}


def can_execute(status: str | None) -> bool:
    """True when a draft in ``status`` may be claimed for execution."""
    return status in EXECUTABLE_STATES


def can_transition(src: str | None, dst: str) -> bool:
    """True when moving a draft from ``src`` to ``dst`` is allowed."""
    if src is None:
        return False
    return dst in ALLOWED_TRANSITIONS.get(src, frozenset())
