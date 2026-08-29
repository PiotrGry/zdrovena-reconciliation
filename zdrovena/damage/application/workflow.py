"""The damaged-shipment workflow: confirm → replacement → shipment → email → close.

This used to live directly in the HTTP router, which meant the rules could only
be exercised by starting FastAPI, and a state transition was one edit away from
being written twice in two handlers (issue #317).

Nothing here imports a web framework or a concrete provider client. Everything
external arrives through ``ports``; refusals leave as domain errors carrying the
operator-facing message, and the router maps those to status codes.
"""

from __future__ import annotations

import logging
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from zdrovena.common import send_attempt
from zdrovena.damage.application.errors import (
    CaseNotFound,
    CorrelationFailed,
    InvalidTransition,
    MailDeliveryFailed,
    MailNotConfigured,
    MailSenderNotAllowed,
    SendBlocked,
)
from zdrovena.damage.application.ports import (
    DamageCaseStore,
    DraftStore,
    MailGateway,
    ShipmentExecutor,
)

logger = logging.getLogger("zdrovena.damage.application.workflow")

#: Statuses from which the replacement workflow has already moved on.
_REPLACEMENT_STARTED = frozenset({"replacement_created", "customer_notified", "closed"})
#: Statuses from which a case may be closed.
_CLOSEABLE = frozenset({"replacement_created", "customer_notified"})

#: Fields cleared when cloning a draft into a replacement. Carrying any of them
#: over would make the new parcel look like it had already been shipped.
_REPLACEMENT_RESET: dict[str, Any] = {
    "shopify_order_id": None,
    "status": "needs_review",
    "tracking_number": None,
    "tracking_company": None,
    "tracking_carrier_id": None,
    "courier_draft_id": None,
    "courier_shipments": [],
    "dispatch_order_id": None,
    "allegro_shipment_id": None,
    "allegro_dispatch_id": None,
    "allegro_pickup_command_id": None,
    "allegro_command_id": None,
    "pickup_ordered": False,
    "shipment_origin": None,
    "error": None,
    "fulfillment_status": None,
    "source_fulfillment_status": None,
    "fulfilled_at": None,
    "fulfilled_by": None,
    "shopify_fulfillment_id": None,
    "allegro_fulfillment_status": None,
    "allegro_marked_processed_at": None,
    "allegro_marked_processed_by": None,
    "fakturownia_invoice_id": None,
    "fakturownia_invoice_number": None,
    "fakturownia_invoice_error": None,
    "fakturownia_invoice_attempts": 0,
    "fakturownia_invoice_attempted_at": None,
}

_REPLACEMENT_DROP = ("allegro_order_shipment_id", "shipment_id", "package_id", "label_url")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DamageWorkflow:
    """One operation per operator action. Every refusal is a domain error."""

    def __init__(
        self,
        *,
        cases: DamageCaseStore,
        drafts: DraftStore,
        executor: ShipmentExecutor | None = None,
        mail: MailGateway | None = None,
        customer_email_from: str = "",
    ) -> None:
        self.cases = cases
        self.drafts = drafts
        self.executor = executor
        self.mail = mail
        self.customer_email_from = customer_email_from

    # ── helpers ───────────────────────────────────────────────────────────────

    def _case(self, case_id: str) -> dict[str, Any]:
        case = self.cases.get_case(case_id)
        if not case:
            raise CaseNotFound(f"Damage case not found: {case_id}")
        return case

    def _save(self, case_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        self.cases.update_case(case_id, fields)
        updated = self.cases.get_case(case_id)
        if not updated:
            raise CaseNotFound(f"Damage case not found: {case_id}")
        return updated

    # ── confirm / ignore ──────────────────────────────────────────────────────

    def confirm(self, case_id: str, *, by: str, note: str | None = None) -> dict[str, Any]:
        case = self._case(case_id)
        if case.get("status") != "needs_review":
            raise InvalidTransition("Case is not waiting for review")
        return self._save(
            case_id,
            {
                "status": "approved",
                "confirmed_at": _now(),
                "confirmed_by": by,
                "operator_note": note,
            },
        )

    def ignore(self, case_id: str, *, by: str) -> dict[str, Any]:
        case = self._case(case_id)
        if case.get("status") in _REPLACEMENT_STARTED:
            raise InvalidTransition("Replacement workflow has already started")
        return self._save(case_id, {"status": "ignored", "ignored_at": _now(), "ignored_by": by})

    def close(self, case_id: str, *, by: str) -> dict[str, Any]:
        case = self._case(case_id)
        if case.get("status") not in _CLOSEABLE:
            raise InvalidTransition("Case is not ready to close")
        return self._save(case_id, {"status": "closed", "closed_at": _now(), "closed_by": by})

    # ── replacement ───────────────────────────────────────────────────────────

    def find_original_draft(self, case: dict[str, Any]) -> dict[str, Any] | None:
        draft_id = case.get("shipping_draft_id")
        if draft_id:
            draft = self.drafts.get_draft(str(draft_id))
            if draft:
                return draft
        tracking = str(case.get("tracking_number") or "").strip().upper()
        for draft in self.drafts.list_drafts(limit=500):
            if str(draft.get("tracking_number") or "").strip().upper() == tracking:
                return draft
        return None

    def clone_replacement_draft(
        self, original: dict[str, Any], case: dict[str, Any]
    ) -> dict[str, Any]:
        now = _now()
        replacement = deepcopy(original)
        replacement.update(
            {
                "id": str(uuid.uuid4()),
                "created_at": now,
                "updated_at": now,
                **_REPLACEMENT_RESET,
                "is_replacement": True,
                "replacement_for_damage_case_id": case["id"],
                "replacement_for_draft_id": original.get("id"),
                "replacement_for_tracking_number": case.get("tracking_number"),
            }
        )
        for key in _REPLACEMENT_DROP:
            replacement.pop(key, None)
        return replacement

    def prepare_replacement(self, case_id: str) -> dict[str, Any]:
        """Idempotent: an already-prepared draft is returned, not recreated."""
        case = self._case(case_id)
        existing_id = case.get("replacement_draft_id")
        if existing_id:
            existing = self.drafts.get_draft(str(existing_id))
            if existing:
                return {"case": case, "draft": existing, "created": False}
        if case.get("status") != "approved":
            raise InvalidTransition("Confirm damage before preparing replacement")
        original = self.find_original_draft(case)
        if not original:
            raise CorrelationFailed("Could not correlate the tracking number with a shipping draft")
        replacement = self.clone_replacement_draft(original, case)
        self.drafts.upsert_draft(replacement)
        updated = self._save(
            case_id,
            {
                "status": "replacement_prepared",
                "shipping_draft_id": original.get("id"),
                "replacement_draft_id": replacement["id"],
                "replacement_prepared_at": _now(),
            },
        )
        return {"case": updated, "draft": replacement, "created": True}

    def create_replacement(self, case_id: str) -> dict[str, Any]:
        case = self._case(case_id)
        replacement_id = case.get("replacement_draft_id")
        if not replacement_id:
            raise InvalidTransition("Prepare the replacement draft first")
        draft = self.drafts.get_draft(str(replacement_id))
        if not draft:
            raise InvalidTransition("Replacement draft no longer exists")
        if draft.get("status") == "created":
            # Already shipped; record the outcome rather than shipping again.
            updated = self._save(
                case_id,
                {
                    "status": "replacement_created",
                    "replacement_tracking_number": draft.get("tracking_number"),
                },
            )
            return {"case": updated, "draft": draft}
        if draft.get("status") == "needs_review":
            self.drafts.update_draft(str(replacement_id), {"status": "pending"})

        if self.executor is None:  # pragma: no cover - wiring error, not a flow
            raise InvalidTransition("No shipment executor configured")
        result = self.executor.execute(str(replacement_id))
        return {"case": self._record_shipment(case_id, result), "draft": result}

    def confirm_replacement(self, case_id: str) -> dict[str, Any]:
        case = self._case(case_id)
        replacement_id = case.get("replacement_draft_id")
        if not replacement_id:
            raise InvalidTransition("Replacement draft is missing")
        if self.executor is None:  # pragma: no cover - wiring error, not a flow
            raise InvalidTransition("No shipment executor configured")
        result = self.executor.confirm(str(replacement_id))
        return {"case": self._record_shipment(case_id, result), "draft": result}

    def _record_shipment(self, case_id: str, result: dict[str, Any]) -> dict[str, Any]:
        """One place deciding what a shipment result means for the case.

        This logic was written out twice in two handlers, which is one edit away
        from the two copies disagreeing about when a case counts as shipped.
        """
        created = result.get("status") == "created"
        return self._save(
            case_id,
            {
                "status": "replacement_created" if created else "replacement_pending",
                "replacement_tracking_number": result.get("tracking_number"),
                "replacement_created_at": _now() if created else None,
            },
        )

    # ── customer email ────────────────────────────────────────────────────────

    def build_customer_email(self, case_id: str) -> dict[str, Any]:
        case = self._case(case_id)
        replacement_id = case.get("replacement_draft_id")
        draft = self.drafts.get_draft(str(replacement_id)) if replacement_id else None
        if not draft or draft.get("status") != "created":
            raise InvalidTransition("Create the replacement parcel first")

        receiver = draft.get("receiver") or {}
        to_address = str(case.get("customer_email") or receiver.get("email") or "").strip()
        if not to_address:
            raise InvalidTransition("Customer email is missing")
        replacement_tracking = str(draft.get("tracking_number") or "").strip()
        if not replacement_tracking:
            raise InvalidTransition("Replacement parcel has no tracking number yet")

        first_name = str(receiver.get("first_name") or "").strip()
        greeting = f"Dzień dobry {first_name}," if first_name else "Dzień dobry,"
        order_number = str(case.get("order_number") or "").strip()
        original_tracking = str(case.get("tracking_number") or "").strip()
        subject_order = f" {order_number}" if order_number else ""
        body = (
            f"{greeting}\n\n"
            f"przewoźnik poinformował nas, że przesyłka {original_tracking}"
            f" z zamówieniem {order_number or 'w naszym sklepie'} została uszkodzona "
            "podczas transportu.\n\n"
            "Przygotowaliśmy dla Ciebie nową paczkę. "
            f"Jej numer śledzenia to {replacement_tracking}.\n\n"
            "Nie musisz podejmować żadnych dodatkowych działań. "
            "Przepraszamy za opóźnienie i niedogodności.\n\n"
            "Pozdrawiamy\n"
            "Zespół HUMIO\n"
            f"{self.customer_email_from}"
        )
        return {
            "from": self.customer_email_from,
            "to": to_address,
            "subject": f"Wysyłamy ponownie Twoje zamówienie{subject_order}",
            "body": body,
            "status": "ready",
            "created_at": _now(),
            "updated_at": _now(),
        }

    def prepare_email_draft(self, case_id: str) -> dict[str, Any]:
        email_draft = self.build_customer_email(case_id)
        updated = self._save(case_id, {"email_draft": email_draft})
        return {"case": updated, "email_draft": email_draft}

    def update_email_draft(self, case_id: str, *, subject: str, body: str) -> dict[str, Any]:
        case = self._case(case_id)
        email_draft = case.get("email_draft")
        if not isinstance(email_draft, dict):
            raise InvalidTransition("Prepare the email draft first")
        if case.get("email_sent_at"):
            raise InvalidTransition("Email has already been sent")
        updated_draft = {**email_draft, "subject": subject, "body": body, "updated_at": _now()}
        updated = self._save(case_id, {"email_draft": updated_draft})
        return {"case": updated, "email_draft": updated_draft}

    def send_customer_email(self, case_id: str, *, by: str) -> dict[str, Any]:
        case = self._case(case_id)
        email_draft = case.get("email_draft")
        if not isinstance(email_draft, dict):
            raise InvalidTransition("Prepare and review the email draft first")
        if case.get("email_sent_at"):
            raise InvalidTransition("Email has already been sent")
        if self.mail is None:
            raise MailNotConfigured("Zoho Mail is not configured")
        if self.customer_email_from.lower() not in self.mail.sender_addresses():
            raise MailSenderNotAllowed(
                f"{self.customer_email_from} is not configured as an active Zoho From address"
            )

        # Refuse before touching SMTP if a previous attempt is unresolved: an
        # ambiguous attempt is the one case where re-sending is the wrong guess.
        allowed, reason = send_attempt.may_send(
            send_attempt.SendAttempt.from_dict(case.get("email_attempt"))
        )
        if not allowed:
            raise SendBlocked(reason or "")

        # Durable BEFORE SMTP, in the same atomic update as the claim, so the
        # message can never be accepted with nothing on disk to say so (#312).
        attempt = send_attempt.begin(
            recipients=[str(email_draft["to"])],
            subject=str(email_draft["subject"]),
            artifact=str(email_draft["body"]),
        )
        if not self.cases.try_claim_email(case_id, attempt.to_dict()):
            raise SendBlocked("Email is already being sent or has already been sent")

        try:
            response = self.mail.send(
                to=str(email_draft["to"]),
                subject=str(email_draft["subject"]),
                body=str(email_draft["body"]),
            )
        except Exception as exc:
            logger.exception("Could not send damage-case email %s", case_id)
            settled = send_attempt.fail(attempt, str(exc)) if _is_definite_refusal(exc) else attempt
            self._save(
                case_id,
                {
                    "email_error": str(exc),
                    "email_last_attempt_at": _now(),
                    "email_sending": False,
                    "email_attempt": settled.to_dict(),
                },
            )
            raise MailDeliveryFailed("Zoho Mail could not send the message") from exc

        data = response.get("data") if isinstance(response, dict) else None
        message_id = data.get("messageId") if isinstance(data, dict) else None
        sent_at = _now()
        updated_draft = {**email_draft, "status": "sent", "sent_at": sent_at}
        updated = self._save(
            case_id,
            {
                "status": "customer_notified",
                "email_draft": updated_draft,
                "email_sent_at": sent_at,
                "email_sent_by": by,
                "email_provider_message_id": message_id,
                "email_error": None,
                "email_sending": False,
                "email_attempt": send_attempt.confirm(
                    attempt, provider_message_id=str(message_id) if message_id else None
                ).to_dict(),
            },
        )
        return {"case": updated, "email_draft": updated_draft, "attempt": attempt}

    def resolve_email_attempt(
        self, case_id: str, *, delivered: bool, by: str, note: str = ""
    ) -> dict[str, Any]:
        case = self._case(case_id)
        attempt = send_attempt.SendAttempt.from_dict(case.get("email_attempt"))
        state = send_attempt.classify(attempt)
        if attempt is None or state not in (send_attempt.UNKNOWN, send_attempt.PENDING):
            raise InvalidTransition("No unresolved email attempt on this case")

        resolved = send_attempt.resolve(attempt, delivered=delivered, by=by, note=note)
        fields: dict[str, Any] = {"email_attempt": resolved.to_dict(), "email_sending": False}
        if delivered:
            fields["email_sent_at"] = case.get("email_sent_at") or _now()
            fields["email_sent_by"] = by
            fields["status"] = "customer_notified"
        return {"case": self._save(case_id, fields), "attempt": resolved.to_dict()}


def _is_definite_refusal(exc: BaseException) -> bool:
    """True only for a refusal the provider actually gave us.

    A timeout or a dead socket is not a failure: the message may still have been
    accepted, so it must stay pending and age into ``unknown`` rather than be
    recorded as a clean failure that invites an automatic retry.

    smtplib is imported lazily so the application layer carries no transport
    dependency at module level.
    """
    import smtplib

    return isinstance(exc, smtplib.SMTPResponseException)
