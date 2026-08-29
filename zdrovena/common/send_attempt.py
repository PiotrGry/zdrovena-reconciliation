"""Crash-safe semantics for sending an email exactly once.

An atomic claim stops two clicks from both sending. It does not make an
irreversible external effect crash-safe, because the dangerous window is not
concurrency -- it is the gap between "SMTP accepted the message" and "we managed
to write that down":

    1. claim taken, marked sending
    2. SMTP accepts the message
    3. the process or container dies before recording it
    4. the claim ages out and is treated as abandoned
    5. the next attempt sends a second email

Step 4 is what converts a crash into a duplicate. A claim that expires quietly is
a claim that assumes nothing happened, and after step 2 that assumption is wrong.

So a stale ``pending`` attempt does not become free again -- it becomes
``unknown``, which blocks automatic sending and waits for a person. Nobody can
tell from our side whether the mail went out; only the recipient's inbox knows.
The safe move is to ask, not to guess, and re-sending is the guess that costs a
customer a duplicate message (issue #312).
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

#: Written before SMTP is contacted. The process intends to send.
PENDING = "pending"
#: A pending attempt that outlived the process holding it. Ambiguous by nature.
UNKNOWN = "unknown"
#: SMTP accepted the message and we recorded it.
CONFIRMED = "confirmed"
#: SMTP refused. Nothing was delivered, so a retry is safe.
FAILED = "failed"

STATES = (PENDING, UNKNOWN, CONFIRMED, FAILED)

#: How long a pending attempt may live before it is treated as ambiguous.
#: Generous on purpose: expiring early turns a slow send into a false unknown.
DEFAULT_STALE_AFTER = timedelta(minutes=10)


class SendAttemptError(RuntimeError):
    """The requested transition is not allowed from the current state."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def fingerprint(*, recipients: list[str] | tuple[str, ...], subject: str, artifact: str) -> str:
    """Identify what was being sent without storing who it went to.

    Recipients and subject are personal data and the artefact reference may be
    long, so only their digest is persisted. It is enough to notice that a
    resumed attempt is not the one that was started.
    """
    parts = [",".join(sorted(str(r).strip().lower() for r in recipients)), subject, artifact]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SendAttempt:
    """One attempt at one irreversible send."""

    id: str
    state: str
    fingerprint: str
    started_at: str
    settled_at: str | None = None
    error: str | None = None
    provider_message_id: str | None = None
    resolved_by: str | None = None
    resolution_note: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = {
            "id": self.id,
            "state": self.state,
            "fingerprint": self.fingerprint,
            "started_at": self.started_at,
            "settled_at": self.settled_at,
            "error": self.error,
            "provider_message_id": self.provider_message_id,
            "resolved_by": self.resolved_by,
            "resolution_note": self.resolution_note,
        }
        data.update(self.extra)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> SendAttempt | None:
        if not data or not data.get("id"):
            return None
        known = {
            "id",
            "state",
            "fingerprint",
            "started_at",
            "settled_at",
            "error",
            "provider_message_id",
            "resolved_by",
            "resolution_note",
        }
        return cls(
            id=str(data["id"]),
            state=str(data.get("state") or UNKNOWN),
            fingerprint=str(data.get("fingerprint") or ""),
            started_at=str(data.get("started_at") or ""),
            settled_at=data.get("settled_at"),
            error=data.get("error"),
            provider_message_id=data.get("provider_message_id"),
            resolved_by=data.get("resolved_by"),
            resolution_note=data.get("resolution_note"),
            extra={k: v for k, v in data.items() if k not in known},
        )


def begin(*, recipients: list[str] | tuple[str, ...], subject: str, artifact: str) -> SendAttempt:
    """The record that must be durable BEFORE SMTP is contacted.

    Writing it afterwards would leave exactly the window this module exists to
    close: an accepted message with nothing on disk to say so.
    """
    return SendAttempt(
        id=str(uuid.uuid4()),
        state=PENDING,
        fingerprint=fingerprint(recipients=recipients, subject=subject, artifact=artifact),
        started_at=_now().isoformat(),
    )


def confirm(attempt: SendAttempt, *, provider_message_id: str | None = None) -> SendAttempt:
    if attempt.state not in (PENDING, UNKNOWN):
        raise SendAttemptError(f"cannot confirm an attempt in state {attempt.state}")
    return SendAttempt(
        **{
            **attempt.__dict__,
            "state": CONFIRMED,
            "settled_at": _now().isoformat(),
            "provider_message_id": provider_message_id or attempt.provider_message_id,
            "error": None,
        }
    )


def fail(attempt: SendAttempt, error: str) -> SendAttempt:
    """Only for a refusal we actually saw. A timeout is not a failure.

    If SMTP never answered, the message may still have been accepted, so that
    case must stay pending and age into ``unknown`` rather than be recorded as
    a clean failure that invites an automatic retry.
    """
    if attempt.state != PENDING:
        raise SendAttemptError(f"cannot fail an attempt in state {attempt.state}")
    return SendAttempt(
        **{
            **attempt.__dict__,
            "state": FAILED,
            "settled_at": _now().isoformat(),
            "error": error[:500],
        }
    )


def classify(
    attempt: SendAttempt | None,
    *,
    now: datetime | None = None,
    stale_after: timedelta = DEFAULT_STALE_AFTER,
) -> str | None:
    """The attempt's effective state, ageing a stranded ``pending`` to ``unknown``."""
    if attempt is None:
        return None
    if attempt.state != PENDING:
        return attempt.state
    try:
        started = datetime.fromisoformat(attempt.started_at)
    except (TypeError, ValueError):
        # An unreadable timestamp cannot prove the attempt is recent, and
        # assuming it is would let a stranded send be retried automatically.
        return UNKNOWN
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    if (now or _now()) - started > stale_after:
        return UNKNOWN
    return PENDING


def may_send(
    attempt: SendAttempt | None,
    *,
    now: datetime | None = None,
    stale_after: timedelta = DEFAULT_STALE_AFTER,
) -> tuple[bool, str | None]:
    """Whether a fresh send may start, and if not, why.

    The reason is operator-facing Polish, matching the rest of the workflow.
    """
    state = classify(attempt, now=now, stale_after=stale_after)
    if state is None or state == FAILED:
        return True, None
    if state == CONFIRMED:
        return False, "Wiadomość została już wysłana."
    if state == PENDING:
        return False, "Wysyłka jest w toku."
    return False, (
        "Poprzednia próba wysyłki nie zakończyła się jednoznacznie — wiadomość mogła "
        "zostać dostarczona. Sprawdź skrzynkę nadawczą i rozstrzygnij próbę, zanim "
        "wyślesz ponownie."
    )


def resolve(attempt: SendAttempt, *, delivered: bool, by: str, note: str = "") -> SendAttempt:
    """An operator's decision about an ambiguous attempt.

    ``delivered=True`` records it as sent, closing the case without a second
    message. ``delivered=False`` records it as failed, which is what unblocks a
    retry. Both are attributed, because this is the one place where a human
    overrides the system's uncertainty.
    """
    if attempt.state not in (UNKNOWN, PENDING):
        raise SendAttemptError(f"cannot resolve an attempt in state {attempt.state}")
    return SendAttempt(
        **{
            **attempt.__dict__,
            "state": CONFIRMED if delivered else FAILED,
            "settled_at": _now().isoformat(),
            "resolved_by": by,
            "resolution_note": note[:500]
            or (
                "operator potwierdził dostarczenie"
                if delivered
                else "operator potwierdził brak wysyłki"
            ),
        }
    )
