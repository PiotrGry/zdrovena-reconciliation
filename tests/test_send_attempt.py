"""Crash semantics for an irreversible send (issue #312).

The fault injection here is at the level where the decision is made: what the
system concludes given a record left behind by a process that died at each
possible point.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from zdrovena.common.send_attempt import (
    CONFIRMED,
    FAILED,
    PENDING,
    UNKNOWN,
    SendAttempt,
    SendAttemptError,
    begin,
    classify,
    confirm,
    fail,
    fingerprint,
    may_send,
    resolve,
)

RECIPIENTS = ["Klient@Example.COM"]


def _begin() -> SendAttempt:
    return begin(recipients=RECIPIENTS, subject="Reklamacja", artifact="sha256-abc")


class TestFingerprint:
    def test_it_stores_no_personal_data(self):
        fp = fingerprint(recipients=RECIPIENTS, subject="Reklamacja", artifact="a")

        assert "example.com" not in fp
        assert "Reklamacja" not in fp
        assert len(fp) == 64

    def test_recipient_order_and_case_do_not_change_it(self):
        a = fingerprint(recipients=["a@x.pl", "b@x.pl"], subject="s", artifact="k")
        b = fingerprint(recipients=["B@X.pl", " a@x.pl "], subject="s", artifact="k")

        assert a == b

    def test_a_different_recipient_changes_it(self):
        a = fingerprint(recipients=["a@x.pl"], subject="s", artifact="k")
        b = fingerprint(recipients=["c@x.pl"], subject="s", artifact="k")

        assert a != b

    def test_a_different_artifact_changes_it(self):
        a = fingerprint(recipients=RECIPIENTS, subject="s", artifact="sha256-aaa")
        b = fingerprint(recipients=RECIPIENTS, subject="s", artifact="sha256-bbb")

        assert a != b


class TestCrashBeforeSmtp:
    """Nothing left the building: retrying is safe."""

    def test_a_refusal_is_recorded_as_failed(self):
        assert fail(_begin(), "550 rejected").state == FAILED

    def test_a_failed_attempt_does_not_block_a_retry(self):
        allowed, reason = may_send(fail(_begin(), "550 rejected"))

        assert allowed is True
        assert reason is None

    def test_no_previous_attempt_does_not_block(self):
        assert may_send(None) == (True, None)


class TestCrashAfterSmtpAccepted:
    """The window this module exists for."""

    def test_a_stranded_pending_attempt_becomes_unknown(self):
        attempt = _begin()
        much_later = datetime.now(timezone.utc) + timedelta(minutes=11)

        assert classify(attempt, now=much_later) == UNKNOWN

    def test_an_unknown_attempt_blocks_automatic_sending(self):
        """A claim that expires quietly assumes nothing happened. After SMTP
        accepted, that assumption is wrong and costs the customer a duplicate."""
        attempt = _begin()
        much_later = datetime.now(timezone.utc) + timedelta(minutes=11)

        allowed, reason = may_send(attempt, now=much_later)

        assert allowed is False
        assert reason is not None
        assert "jednoznacz" in reason

    def test_a_fresh_pending_attempt_blocks_too_but_reads_as_in_progress(self):
        allowed, reason = may_send(_begin())

        assert allowed is False
        assert "w toku" in reason

    def test_a_confirmed_attempt_blocks(self):
        allowed, reason = may_send(confirm(_begin(), provider_message_id="m-1"))

        assert allowed is False
        assert "już wysłana" in reason

    def test_an_unreadable_timestamp_is_treated_as_unknown(self):
        """It cannot prove the attempt is recent, and assuming so would let a
        stranded send be retried automatically."""
        attempt = SendAttempt(id="x", state=PENDING, fingerprint="f", started_at="wczoraj")

        assert classify(attempt) == UNKNOWN


class TestCrashBeforeTheFinalWrite:
    def test_an_unknown_attempt_can_still_be_confirmed(self):
        """Recovery after the fact: the send did happen, we just lost the write."""
        attempt = SendAttempt(
            id="x", state=UNKNOWN, fingerprint="f", started_at="2026-01-01T00:00:00+00:00"
        )

        assert confirm(attempt).state == CONFIRMED

    def test_a_timeout_must_not_be_recorded_as_a_clean_failure(self):
        """`fail` is only for a refusal we saw. Anything else stays pending and
        ages into unknown, instead of inviting an automatic retry."""
        settled = confirm(_begin())

        with pytest.raises(SendAttemptError):
            fail(settled, "timeout")


class TestOperatorRecovery:
    def test_confirming_delivery_closes_the_case_without_a_second_message(self):
        attempt = SendAttempt(
            id="x", state=UNKNOWN, fingerprint="f", started_at="2026-01-01T00:00:00+00:00"
        )

        resolved = resolve(attempt, delivered=True, by="owner@example.com")

        assert resolved.state == CONFIRMED
        assert may_send(resolved)[0] is False

    def test_confirming_non_delivery_unblocks_a_retry(self):
        attempt = SendAttempt(
            id="x", state=UNKNOWN, fingerprint="f", started_at="2026-01-01T00:00:00+00:00"
        )

        resolved = resolve(attempt, delivered=False, by="owner@example.com")

        assert resolved.state == FAILED
        assert may_send(resolved)[0] is True

    def test_the_decision_is_attributed(self):
        attempt = SendAttempt(
            id="x", state=UNKNOWN, fingerprint="f", started_at="2026-01-01T00:00:00+00:00"
        )

        resolved = resolve(
            attempt, delivered=True, by="owner@example.com", note="sprawdzone w Zoho"
        )

        assert resolved.resolved_by == "owner@example.com"
        assert resolved.resolution_note == "sprawdzone w Zoho"

    def test_a_settled_attempt_cannot_be_resolved_again(self):
        with pytest.raises(SendAttemptError):
            resolve(confirm(_begin()), delivered=False, by="owner@example.com")


class TestRoundTrip:
    def test_the_record_survives_persistence(self):
        attempt = confirm(_begin(), provider_message_id="m-9")

        restored = SendAttempt.from_dict(attempt.to_dict())

        assert restored == attempt

    def test_an_absent_record_restores_as_nothing(self):
        assert SendAttempt.from_dict(None) is None
        assert SendAttempt.from_dict({}) is None

    def test_a_record_without_a_state_is_treated_as_unknown(self):
        """A half-written record must not read as 'safe to send'."""
        restored = SendAttempt.from_dict({"id": "x", "started_at": "2026-01-01T00:00:00+00:00"})

        assert restored.state == UNKNOWN
        assert may_send(restored)[0] is False
