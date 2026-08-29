"""Operator-controlled workflow for damaged shipments."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from zdrovena.api import shipping_execution_composition as execution_composition
from zdrovena.api.auth import (
    Principal,
    require_shipment_mgr_or_above,
    require_viewer_or_above,
)
from zdrovena.api.damage_detection import (
    build_apaczka_lookup_client,
    build_inpost_lookup_client,
    build_zoho_client,
    scan_allegro_damage_cases,
    scan_zoho_damage_cases,
)
from zdrovena.api.deps import DamageStoreDep, ShippingStoreDep, StorageDep
from zdrovena.api.models import (
    DamageCaseModel,
    DamageCasesResponse,
    DamageCaseWithDraftResponse,
    DamageRefreshResponse,
    DamageSummaryResponse,
)
from zdrovena.common.config import KEYCHAIN_SERVICE_ZOHO_SMTP
from zdrovena.common.events import log_event
from zdrovena.common.secrets import get_secret
from zdrovena.damage.application import (
    CaseNotFound,
    CorrelationFailed,
    DamageWorkflow,
    DamageWorkflowError,
    InvalidTransition,
    MailNotConfigured,
    MailSenderNotAllowed,
    SendBlocked,
)
from zdrovena.damage.application.errors import MailDeliveryFailed
from zdrovena.month_closing.config import ZOHO_EMAIL
from zdrovena.month_closing.email_service import EmailService

logger = logging.getLogger("zdrovena.api.routers.damage")

router = APIRouter(tags=["damaged shipments"])

CUSTOMER_EMAIL_FROM = "info@wodahumio.pl"


class ConfirmDamageRequest(BaseModel):
    note: str | None = Field(default=None, max_length=1000)


class EmailDraftUpdate(BaseModel):
    subject: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=20_000)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _case_or_404(damage_store: Any, case_id: str) -> dict[str, Any]:
    case = damage_store.get_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Damage case not found")
    return case


def _save_case(damage_store: Any, case_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    fields["updated_at"] = _now()
    if not damage_store.update_case(case_id, fields):
        raise HTTPException(status_code=404, detail="Damage case not found")
    return _case_or_404(damage_store, case_id)


def _send_email_with_configured_zoho_smtp(
    *, to_address: str, subject: str, content: str
) -> dict[str, Any]:
    """Send through the same Zoho SMTP path used by month-close reports."""
    smtp_password = get_secret(KEYCHAIN_SERVICE_ZOHO_SMTP, required=False)
    if not smtp_password:
        raise RuntimeError("Zoho SMTP password is not configured")
    EmailService(
        smtp_password=smtp_password,
        sender_email=ZOHO_EMAIL,
        from_email=CUSTOMER_EMAIL_FROM,
    ).send_report(to_address, subject, content)
    return {"data": {"transport": "smtp"}}


@router.get(
    "/damage-cases",
    summary="List damaged-shipment cases",
    response_model=DamageCasesResponse,
)
def list_damage_cases(
    damage_store: DamageStoreDep,
    principal: Annotated[Principal, Depends(require_viewer_or_above)],
) -> dict[str, Any]:
    del principal
    cases = [
        case
        for case in damage_store.list_cases(limit=500)
        if case.get("classification") == "damage"
    ]
    return {
        "cases": cases,
        "needs_review": sum(case.get("status") == "needs_review" for case in cases),
    }


@router.get(
    "/damage-cases/summary",
    summary="Count damage cases requiring attention",
    response_model=DamageSummaryResponse,
)
def damage_case_summary(
    damage_store: DamageStoreDep,
    principal: Annotated[Principal, Depends(require_viewer_or_above)],
) -> dict[str, int]:
    del principal
    return {"needs_review": damage_store.count_needs_review()}


@router.get(
    "/damage-cases/{case_id}",
    summary="Get a damaged-shipment case",
    response_model=DamageCaseModel,
)
def get_damage_case(
    case_id: str,
    damage_store: DamageStoreDep,
    principal: Annotated[Principal, Depends(require_viewer_or_above)],
) -> dict[str, Any]:
    del principal
    return _case_or_404(damage_store, case_id)


@router.post(
    "/damage-cases/refresh",
    summary="Fetch Allegro and Zoho damage signals",
    response_model=DamageRefreshResponse,
)
def refresh_damage_cases(
    damage_store: DamageStoreDep,
    shipping_store: ShippingStoreDep,
    storage: StorageDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
) -> dict[str, Any]:
    del principal
    result: dict[str, Any] = {
        "allegro": {"skipped": "not_configured"},
        "zoho": {"skipped": "not_configured"},
    }
    try:
        allegro = execution_composition.get_allegro_client()
        if allegro is not None:
            result["allegro"] = scan_allegro_damage_cases(
                client=allegro,
                shipping_store=shipping_store,
                damage_store=damage_store,
            )
    except Exception as exc:
        logger.exception("Allegro damage scan failed")
        result["allegro"] = {"error": str(exc)}
    try:
        zoho = build_zoho_client()
        if zoho is not None:
            result["zoho"] = scan_zoho_damage_cases(
                client=zoho,
                shipping_store=shipping_store,
                damage_store=damage_store,
                inpost_client=build_inpost_lookup_client(),
                apaczka_client=build_apaczka_lookup_client(storage),
            )
    except Exception as exc:
        logger.exception("Zoho damage scan failed")
        result["zoho"] = {"error": str(exc)}
    result["needs_review"] = damage_store.count_needs_review()
    return result


# ── Workflow handlers ─────────────────────────────────────────────────────────
#
# These map a request onto DamageWorkflow and translate domain errors into
# status codes. The rules themselves live in zdrovena.damage.application, so they
# can be exercised without starting HTTP and a state transition cannot be written
# twice in two handlers (issue #317).


def _workflow(
    damage_store: Any,
    shipping_store: Any = None,
    storage: Any = None,
) -> DamageWorkflow:
    executor = _ShipmentExecutorAdapter(shipping_store, storage) if shipping_store else None
    zoho = build_zoho_client()
    return DamageWorkflow(
        cases=damage_store,
        drafts=shipping_store,
        executor=executor,
        mail=_ZohoMailAdapter(zoho) if zoho is not None else None,
        customer_email_from=CUSTOMER_EMAIL_FROM,
    )


class _ShipmentExecutorAdapter:
    """Keeps provider and HTTP mapping on this side of the port.

    The composition helpers raise FastAPI exceptions; letting them travel
    outward unchanged is what allows the application layer to stay free of any
    web framework while the existing responses stay identical.
    """

    def __init__(self, shipping_store: Any, storage: Any) -> None:
        self.shipping_store = shipping_store
        self.storage = storage

    def execute(self, draft_id: str) -> dict[str, Any]:
        try:
            return execution_composition.execute_shipping_draft(
                draft_id, self.shipping_store, self.storage
            )
        except execution_composition.EXECUTION_APPLICATION_HTTP_ERRORS as exc:
            execution_composition.raise_execution_http_exception(exc)
            raise  # pragma: no cover - the helper always raises

    def confirm(self, draft_id: str) -> dict[str, Any]:
        try:
            confirmation = execution_composition.confirm_shipping_draft(
                draft_id, self.shipping_store
            )
        except execution_composition.ConfirmationError as exc:
            execution_composition.raise_confirmation_http_exception(exc)
            raise  # pragma: no cover - the helper always raises
        return confirmation.payload


class _ZohoMailAdapter:
    def __init__(self, zoho: Any) -> None:
        self.zoho = zoho

    def sender_addresses(self) -> set[str]:
        return self.zoho.sender_addresses()

    def send(self, *, to: str, subject: str, body: str) -> dict[str, Any] | None:
        return _send_email_with_configured_zoho_smtp(to_address=to, subject=subject, content=body)


#: Domain refusal → HTTP status. One table instead of a status code repeated
#: at every raise site.
_ERROR_STATUS: dict[type, int] = {
    CaseNotFound: 404,
    InvalidTransition: 409,
    CorrelationFailed: 409,
    MailSenderNotAllowed: 409,
    SendBlocked: 409,
    MailNotConfigured: 503,
    MailDeliveryFailed: 502,
}


def _http(exc: DamageWorkflowError) -> HTTPException:
    for error_type, code in _ERROR_STATUS.items():
        if isinstance(exc, error_type):
            return HTTPException(status_code=code, detail=str(exc))
    return HTTPException(status_code=409, detail=str(exc))  # pragma: no cover


@router.post(
    "/damage-cases/{case_id}/confirm",
    summary="Confirm parcel damage",
    response_model=DamageCaseModel,
)
def confirm_damage_case(
    case_id: str,
    body: ConfirmDamageRequest,
    damage_store: DamageStoreDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
) -> dict[str, Any]:
    try:
        return _workflow(damage_store).confirm(case_id, by=principal.email, note=body.note)
    except DamageWorkflowError as exc:
        raise _http(exc) from exc


@router.post(
    "/damage-cases/{case_id}/ignore",
    summary="Ignore a false-positive case",
    response_model=DamageCaseModel,
)
def ignore_damage_case(
    case_id: str,
    damage_store: DamageStoreDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
) -> dict[str, Any]:
    try:
        return _workflow(damage_store).ignore(case_id, by=principal.email)
    except DamageWorkflowError as exc:
        raise _http(exc) from exc


@router.post(
    "/damage-cases/{case_id}/prepare-replacement",
    summary="Prepare a replacement draft without creating a courier shipment",
    response_model=DamageCaseWithDraftResponse,
)
def prepare_replacement(
    case_id: str,
    damage_store: DamageStoreDep,
    shipping_store: ShippingStoreDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
) -> dict[str, Any]:
    del principal
    try:
        return _workflow(damage_store, shipping_store).prepare_replacement(case_id)
    except DamageWorkflowError as exc:
        raise _http(exc) from exc


@router.post(
    "/damage-cases/{case_id}/create-replacement",
    summary="Create the previously prepared courier shipment",
    response_model=DamageCaseWithDraftResponse,
)
def create_replacement(
    case_id: str,
    damage_store: DamageStoreDep,
    shipping_store: ShippingStoreDep,
    storage: StorageDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
) -> dict[str, Any]:
    del principal
    try:
        return _workflow(damage_store, shipping_store, storage).create_replacement(case_id)
    except DamageWorkflowError as exc:
        raise _http(exc) from exc


@router.post(
    "/damage-cases/{case_id}/confirm-replacement",
    summary="Poll a pending Allegro replacement shipment",
    response_model=DamageCaseWithDraftResponse,
)
def confirm_replacement(
    case_id: str,
    damage_store: DamageStoreDep,
    shipping_store: ShippingStoreDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
) -> dict[str, Any]:
    del principal
    try:
        return _workflow(damage_store, shipping_store).confirm_replacement(case_id)
    except DamageWorkflowError as exc:
        raise _http(exc) from exc


@router.post(
    "/damage-cases/{case_id}/email-draft",
    summary="Prepare customer email",
    response_model=DamageCaseWithDraftResponse,
)
def prepare_email_draft(
    case_id: str,
    damage_store: DamageStoreDep,
    shipping_store: ShippingStoreDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
) -> dict[str, Any]:
    del principal
    try:
        return _workflow(damage_store, shipping_store).prepare_email_draft(case_id)
    except DamageWorkflowError as exc:
        raise _http(exc) from exc


@router.patch(
    "/damage-cases/{case_id}/email-draft",
    summary="Edit customer email draft",
    response_model=DamageCaseWithDraftResponse,
)
def update_email_draft(
    case_id: str,
    body: EmailDraftUpdate,
    damage_store: DamageStoreDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
) -> dict[str, Any]:
    del principal
    try:
        return _workflow(damage_store).update_email_draft(
            case_id, subject=body.subject, body=body.body
        )
    except DamageWorkflowError as exc:
        raise _http(exc) from exc


@router.post(
    "/damage-cases/{case_id}/send-email",
    summary="Send approved customer email",
    response_model=DamageCaseWithDraftResponse,
)
def send_customer_email(
    case_id: str,
    damage_store: DamageStoreDep,
    shipping_store: ShippingStoreDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
) -> dict[str, Any]:
    try:
        result = _workflow(damage_store, shipping_store).send_customer_email(
            case_id, by=principal.email
        )
    except DamageWorkflowError as exc:
        raise _http(exc) from exc
    attempt = result.pop("attempt", None)
    if attempt is not None:
        log_event(
            "damage.email_sent",
            case_id=case_id,
            attempt_id=attempt.id,
            fingerprint=attempt.fingerprint,
        )
    return result


class EmailAttemptResolution(BaseModel):
    delivered: bool
    note: str = ""


@router.post(
    "/damage-cases/{case_id}/resolve-email-attempt",
    summary="Resolve an email attempt whose outcome is unknown",
    responses={409: {"description": "No unresolved attempt to resolve"}},
    response_model=DamageCaseWithDraftResponse,
)
def resolve_email_attempt(
    case_id: str,
    body: EmailAttemptResolution,
    damage_store: DamageStoreDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
) -> dict[str, Any]:
    """Record what actually happened to an ambiguous send.

    Only a person can settle this: from our side an accepted-then-lost message
    and a never-sent one look identical. `delivered=true` closes the case
    without a second message; `delivered=false` is what unblocks a retry.
    """
    by = principal.email or principal.sub
    try:
        result = _workflow(damage_store).resolve_email_attempt(
            case_id, delivered=body.delivered, by=by, note=body.note
        )
    except DamageWorkflowError as exc:
        raise _http(exc) from exc
    log_event(
        "damage.email_attempt_resolved",
        case_id=case_id,
        attempt_id=result["attempt"]["id"],
        delivered=body.delivered,
        resolved_by=by,
    )
    return result


@router.post(
    "/damage-cases/{case_id}/close",
    summary="Close a damage case",
    response_model=DamageCaseModel,
)
def close_damage_case(
    case_id: str,
    damage_store: DamageStoreDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
) -> dict[str, Any]:
    try:
        return _workflow(damage_store).close(case_id, by=principal.email)
    except DamageWorkflowError as exc:
        raise _http(exc) from exc
