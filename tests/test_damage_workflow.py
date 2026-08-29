"""The damage workflow, exercised without starting HTTP (issue #317).

Every rule here used to be reachable only through FastAPI, which meant a test
of a state transition had to boot an app, build a principal and route a request.
"""

from __future__ import annotations

import smtplib
from typing import Any

import pytest

from zdrovena.damage.application import (
    CaseNotFound,
    CorrelationFailed,
    DamageWorkflow,
    InvalidTransition,
    MailNotConfigured,
    MailSenderNotAllowed,
    SendBlocked,
)
from zdrovena.damage.application.errors import MailDeliveryFailed

FROM = "info@wodahumio.pl"


class _Cases:
    def __init__(self, case: dict[str, Any] | None = None) -> None:
        self.store: dict[str, dict[str, Any]] = {}
        if case:
            self.store[case["id"]] = dict(case)
        self.claims = 0

    def get_case(self, case_id: str):
        case = self.store.get(case_id)
        return dict(case) if case else None

    def update_case(self, case_id: str, fields: dict[str, Any]):
        self.store.setdefault(case_id, {"id": case_id}).update(fields)
        return self.store[case_id]

    def try_claim_email(self, case_id: str, attempt=None) -> bool:
        self.claims += 1
        if attempt is not None:
            self.store[case_id]["email_attempt"] = attempt
        return True


class _Drafts:
    def __init__(self, drafts: list[dict[str, Any]] | None = None) -> None:
        self.store = {d["id"]: dict(d) for d in (drafts or [])}

    def get_draft(self, draft_id: str):
        draft = self.store.get(draft_id)
        return dict(draft) if draft else None

    def upsert_draft(self, record: dict[str, Any]) -> None:
        self.store[record["id"]] = dict(record)

    def update_draft(self, draft_id: str, fields: dict[str, Any]):
        self.store[draft_id].update(fields)
        return self.store[draft_id]

    def list_drafts(self, limit: int = 200):
        return [dict(d) for d in list(self.store.values())[:limit]]


class _Executor:
    def __init__(self, result: dict[str, Any] | None = None, exc: Exception | None = None) -> None:
        self.result = result or {"id": "r-1", "status": "created", "tracking_number": "T-9"}
        self.exc = exc
        self.calls: list[str] = []

    def execute(self, draft_id: str):
        self.calls.append(draft_id)
        if self.exc:
            raise self.exc
        return self.result

    def confirm(self, draft_id: str):
        self.calls.append(draft_id)
        return self.result


class _Mail:
    def __init__(self, senders=(FROM,), exc: Exception | None = None) -> None:
        self.senders = set(senders)
        self.exc = exc
        self.sent: list[dict[str, str]] = []

    def sender_addresses(self):
        return self.senders

    def send(self, *, to: str, subject: str, body: str):
        if self.exc:
            raise self.exc
        self.sent.append({"to": to, "subject": subject, "body": body})
        return {"data": {"messageId": "m-1"}}


def _case(**overrides) -> dict[str, Any]:
    return {
        "id": "c-1",
        "status": "needs_review",
        "tracking_number": "A0052ORIG",
        "order_number": "1648",
        "customer_email": "jan@example.com",
        **overrides,
    }


def _draft(**overrides) -> dict[str, Any]:
    return {
        "id": "original",
        "status": "created",
        "tracking_number": "A0052ORIG",
        "receiver": {"first_name": "Jan", "email": "jan@example.com"},
        "courier_shipments": [{"id": 1}],
        "label_url": "https://x",
        **overrides,
    }


def _wf(case=None, drafts=None, executor=None, mail=None) -> DamageWorkflow:
    return DamageWorkflow(
        cases=_Cases(case),
        drafts=_Drafts(drafts),
        executor=executor,
        mail=mail,
        customer_email_from=FROM,
    )


class TestConfirmAndIgnore:
    def test_confirm_moves_a_reviewed_case_to_approved(self):
        wf = _wf(_case())

        updated = wf.confirm("c-1", by="op@example.com", note="zdjęcia ok")

        assert updated["status"] == "approved"
        assert updated["confirmed_by"] == "op@example.com"
        assert updated["operator_note"] == "zdjęcia ok"

    def test_confirm_refuses_a_case_that_is_not_waiting(self):
        wf = _wf(_case(status="approved"))

        with pytest.raises(InvalidTransition, match="not waiting for review"):
            wf.confirm("c-1", by="op@example.com")

    def test_a_missing_case_is_its_own_error(self):
        with pytest.raises(CaseNotFound):
            _wf().confirm("nope", by="op@example.com")

    @pytest.mark.parametrize("status", ["replacement_created", "customer_notified", "closed"])
    def test_ignore_refuses_once_the_replacement_started(self, status):
        wf = _wf(_case(status=status))

        with pytest.raises(InvalidTransition, match="already started"):
            wf.ignore("c-1", by="op@example.com")

    def test_close_requires_a_shipped_or_notified_case(self):
        with pytest.raises(InvalidTransition, match="not ready to close"):
            _wf(_case(status="approved")).close("c-1", by="op@example.com")

    def test_close_records_who_closed_it(self):
        updated = _wf(_case(status="replacement_created")).close("c-1", by="op@example.com")

        assert updated["status"] == "closed"
        assert updated["closed_by"] == "op@example.com"


class TestReplacementPreparation:
    def test_it_correlates_by_tracking_number_when_no_draft_is_linked(self):
        wf = _wf(_case(status="approved"), [_draft()])

        result = wf.prepare_replacement("c-1")

        assert result["created"] is True
        assert result["draft"]["replacement_for_draft_id"] == "original"

    def test_an_uncorrelatable_case_is_refused(self):
        wf = _wf(_case(status="approved"), [_draft(tracking_number="SOMETHING-ELSE")])

        with pytest.raises(CorrelationFailed):
            wf.prepare_replacement("c-1")

    def test_preparation_is_idempotent(self):
        """A second click must return the same draft, not make another parcel."""
        wf = _wf(_case(status="approved"), [_draft()])
        first = wf.prepare_replacement("c-1")

        second = wf.prepare_replacement("c-1")

        assert second["created"] is False
        assert second["draft"]["id"] == first["draft"]["id"]

    def test_it_refuses_before_the_damage_is_confirmed(self):
        wf = _wf(_case(status="needs_review"), [_draft()])

        with pytest.raises(InvalidTransition, match="Confirm damage"):
            wf.prepare_replacement("c-1")

    def test_the_clone_carries_no_shipment_identity(self):
        """Anything left over would make the new parcel look already shipped."""
        wf = _wf(_case(status="approved"), [_draft()])

        replacement = wf.prepare_replacement("c-1")["draft"]

        assert replacement["id"] != "original"
        assert replacement["status"] == "needs_review"
        assert replacement["tracking_number"] is None
        assert replacement["courier_shipments"] == []
        assert "label_url" not in replacement
        assert replacement["is_replacement"] is True


class TestReplacementShipment:
    def _prepared(self, executor):
        wf = _wf(_case(status="approved"), [_draft()], executor=executor)
        wf.prepare_replacement("c-1")
        return wf

    def test_a_created_shipment_marks_the_case_shipped(self):
        executor = _Executor()
        result = self._prepared(executor).create_replacement("c-1")

        assert result["case"]["status"] == "replacement_created"
        assert result["case"]["replacement_tracking_number"] == "T-9"
        assert result["case"]["replacement_created_at"]

    def test_a_pending_shipment_leaves_the_case_pending(self):
        executor = _Executor({"id": "r-1", "status": "pending", "tracking_number": None})

        result = self._prepared(executor).create_replacement("c-1")

        assert result["case"]["status"] == "replacement_pending"
        assert result["case"]["replacement_created_at"] is None

    def test_an_already_created_draft_is_not_shipped_again(self):
        """Re-clicking must record the outcome, not make a second parcel."""
        executor = _Executor()
        wf = self._prepared(executor)
        replacement_id = wf.cases.get_case("c-1")["replacement_draft_id"]
        wf.drafts.update_draft(replacement_id, {"status": "created", "tracking_number": "T-1"})

        result = wf.create_replacement("c-1")

        assert executor.calls == []
        assert result["case"]["status"] == "replacement_created"

    def test_shipping_without_a_prepared_draft_is_refused(self):
        with pytest.raises(InvalidTransition, match="Prepare the replacement draft first"):
            _wf(_case(status="approved"), executor=_Executor()).create_replacement("c-1")

    def test_executor_failures_travel_outward_untouched(self):
        """Provider and HTTP mapping stay on the adapter's side of the port."""
        executor = _Executor(exc=RuntimeError("courier exploded"))

        with pytest.raises(RuntimeError, match="courier exploded"):
            self._prepared(executor).create_replacement("c-1")


class TestCustomerEmail:
    def _shipped(self, mail=None):
        wf = _wf(
            _case(status="replacement_created", replacement_draft_id="rep"),
            [_draft(id="rep", tracking_number="T-9")],
            mail=mail,
        )
        return wf

    def test_the_draft_names_both_tracking_numbers(self):
        result = self._shipped().prepare_email_draft("c-1")

        body = result["email_draft"]["body"]
        assert "A0052ORIG" in body
        assert "T-9" in body
        assert result["email_draft"]["to"] == "jan@example.com"

    def test_a_parcel_without_tracking_cannot_be_announced(self):
        wf = _wf(
            _case(status="replacement_created", replacement_draft_id="rep"),
            [_draft(id="rep", status="pending", tracking_number=None)],
        )

        with pytest.raises(InvalidTransition, match="Create the replacement parcel first"):
            wf.prepare_email_draft("c-1")

    def test_editing_after_sending_is_refused(self):
        wf = _wf(
            _case(email_draft={"subject": "s", "body": "b"}, email_sent_at="2026-08-01T00:00:00Z")
        )

        with pytest.raises(InvalidTransition, match="already been sent"):
            wf.update_email_draft("c-1", subject="x", body="y")

    def test_sending_needs_a_configured_provider(self):
        wf = _wf(_case(email_draft={"to": "a@b.pl", "subject": "s", "body": "b"}))

        with pytest.raises(MailNotConfigured):
            wf.send_customer_email("c-1", by="op@example.com")

    def test_sending_needs_an_allowed_from_address(self):
        wf = _wf(
            _case(email_draft={"to": "a@b.pl", "subject": "s", "body": "b"}),
            mail=_Mail(senders=("someone-else@example.com",)),
        )

        with pytest.raises(MailSenderNotAllowed):
            wf.send_customer_email("c-1", by="op@example.com")

    def test_a_successful_send_notifies_and_confirms(self):
        mail = _Mail()
        wf = _wf(_case(email_draft={"to": "a@b.pl", "subject": "s", "body": "b"}), mail=mail)

        result = wf.send_customer_email("c-1", by="op@example.com")

        assert result["case"]["status"] == "customer_notified"
        assert result["case"]["email_attempt"]["state"] == "confirmed"
        assert len(mail.sent) == 1

    def test_a_refusal_is_recorded_as_failed_and_retryable(self):
        mail = _Mail(exc=smtplib.SMTPResponseException(550, b"nope"))
        wf = _wf(_case(email_draft={"to": "a@b.pl", "subject": "s", "body": "b"}), mail=mail)

        with pytest.raises(MailDeliveryFailed):
            wf.send_customer_email("c-1", by="op@example.com")

        assert wf.cases.get_case("c-1")["email_attempt"]["state"] == "failed"

    def test_a_timeout_stays_pending(self):
        """We never saw a refusal, so the message may have been delivered."""
        mail = _Mail(exc=TimeoutError("no answer"))
        wf = _wf(_case(email_draft={"to": "a@b.pl", "subject": "s", "body": "b"}), mail=mail)

        with pytest.raises(MailDeliveryFailed):
            wf.send_customer_email("c-1", by="op@example.com")

        assert wf.cases.get_case("c-1")["email_attempt"]["state"] == "pending"

    def test_an_unresolved_attempt_blocks_a_second_send(self):
        mail = _Mail()
        wf = _wf(
            _case(
                email_draft={"to": "a@b.pl", "subject": "s", "body": "b"},
                email_attempt={
                    "id": "a-1",
                    "state": "pending",
                    "fingerprint": "f" * 64,
                    "started_at": "2020-01-01T00:00:00+00:00",
                },
            ),
            mail=mail,
        )

        with pytest.raises(SendBlocked):
            wf.send_customer_email("c-1", by="op@example.com")

        assert mail.sent == []
