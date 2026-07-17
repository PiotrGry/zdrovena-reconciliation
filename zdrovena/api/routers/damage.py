"""Operator-controlled workflow for damaged shipments."""

from __future__ import annotations

import json
import logging
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

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
from zdrovena.common.config import KEYCHAIN_SERVICE_ZOHO_SMTP
from zdrovena.common.secrets import get_secret
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


def _find_original_draft(case: dict[str, Any], shipping_store: Any) -> dict[str, Any] | None:
    draft_id = case.get("shipping_draft_id")
    if draft_id:
        draft = shipping_store.get_draft(str(draft_id))
        if draft:
            return draft
    tracking = str(case.get("tracking_number") or "").strip().upper()
    for draft in shipping_store.list_drafts(limit=500):
        if str(draft.get("tracking_number") or "").strip().upper() == tracking:
            return draft
    return None


def _clone_replacement_draft(original: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
    now = _now()
    replacement = deepcopy(original)
    replacement.update(
        {
            "id": str(uuid.uuid4()),
            "created_at": now,
            "updated_at": now,
            "shopify_order_id": None,
            "status": "needs_review",
            "tracking_number": None,
            "tracking_company": None,
            "tracking_carrier_id": None,
            "courier_draft_id": None,
            "dispatch_order_id": None,
            "pickup_ordered": False,
            "error": None,
            "fulfilled_at": None,
            "shopify_fulfillment_id": None,
            "fakturownia_invoice_id": None,
            "fakturownia_invoice_number": None,
            "fakturownia_invoice_error": None,
            "fakturownia_invoice_attempts": 0,
            "fakturownia_invoice_attempted_at": None,
            "is_replacement": True,
            "replacement_for_damage_case_id": case["id"],
            "replacement_for_draft_id": original.get("id"),
            "replacement_for_tracking_number": case.get("tracking_number"),
        }
    )
    for key in (
        "allegro_command_id",
        "allegro_order_shipment_id",
        "shipment_id",
        "package_id",
        "label_url",
    ):
        replacement.pop(key, None)
    return replacement


def _build_customer_email(case: dict[str, Any], draft: dict[str, Any]) -> dict[str, Any]:
    receiver = draft.get("receiver") or {}
    to_address = str(case.get("customer_email") or receiver.get("email") or "").strip()
    if not to_address:
        raise HTTPException(status_code=409, detail="Customer email is missing")
    first_name = str(receiver.get("first_name") or "").strip()
    greeting = f"Dzień dobry {first_name}," if first_name else "Dzień dobry,"
    order_number = str(case.get("order_number") or "").strip()
    original_tracking = str(case.get("tracking_number") or "").strip()
    replacement_tracking = str(draft.get("tracking_number") or "").strip()
    if not replacement_tracking:
        raise HTTPException(
            status_code=409,
            detail="Replacement parcel has no tracking number yet",
        )
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
        f"{CUSTOMER_EMAIL_FROM}"
    )
    return {
        "from": CUSTOMER_EMAIL_FROM,
        "to": to_address,
        "subject": f"Wysyłamy ponownie Twoje zamówienie{subject_order}",
        "body": body,
        "status": "ready",
        "created_at": _now(),
        "updated_at": _now(),
    }


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


@router.get("/damage-cases", summary="List damaged-shipment cases")
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


@router.get("/damage-cases/summary", summary="Count damage cases requiring attention")
def damage_case_summary(
    damage_store: DamageStoreDep,
    principal: Annotated[Principal, Depends(require_viewer_or_above)],
) -> dict[str, int]:
    del principal
    return {"needs_review": damage_store.count_needs_review()}


@router.get("/damage-cases/{case_id}", summary="Get a damaged-shipment case")
def get_damage_case(
    case_id: str,
    damage_store: DamageStoreDep,
    principal: Annotated[Principal, Depends(require_viewer_or_above)],
) -> dict[str, Any]:
    del principal
    return _case_or_404(damage_store, case_id)


@router.post("/damage-cases/refresh", summary="Fetch Allegro and Zoho damage signals")
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
        from zdrovena.api.routers.webhooks import _get_allegro_client

        allegro = _get_allegro_client()
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


@router.post("/damage-cases/{case_id}/confirm", summary="Confirm parcel damage")
def confirm_damage_case(
    case_id: str,
    body: ConfirmDamageRequest,
    damage_store: DamageStoreDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
) -> dict[str, Any]:
    case = _case_or_404(damage_store, case_id)
    if case.get("status") != "needs_review":
        raise HTTPException(status_code=409, detail="Case is not waiting for review")
    return _save_case(
        damage_store,
        case_id,
        {
            "status": "approved",
            "confirmed_at": _now(),
            "confirmed_by": principal.email,
            "operator_note": body.note,
        },
    )


@router.post("/damage-cases/{case_id}/ignore", summary="Ignore a false-positive case")
def ignore_damage_case(
    case_id: str,
    damage_store: DamageStoreDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
) -> dict[str, Any]:
    case = _case_or_404(damage_store, case_id)
    if case.get("status") in {"replacement_created", "customer_notified", "closed"}:
        raise HTTPException(status_code=409, detail="Replacement workflow has already started")
    return _save_case(
        damage_store,
        case_id,
        {"status": "ignored", "ignored_at": _now(), "ignored_by": principal.email},
    )


@router.post(
    "/damage-cases/{case_id}/prepare-replacement",
    summary="Prepare a replacement draft without creating a courier shipment",
)
def prepare_replacement(
    case_id: str,
    damage_store: DamageStoreDep,
    shipping_store: ShippingStoreDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
) -> dict[str, Any]:
    del principal
    case = _case_or_404(damage_store, case_id)
    existing_id = case.get("replacement_draft_id")
    if existing_id:
        existing = shipping_store.get_draft(str(existing_id))
        if existing:
            return {"case": case, "draft": existing, "created": False}
    if case.get("status") != "approved":
        raise HTTPException(status_code=409, detail="Confirm damage before preparing replacement")
    original = _find_original_draft(case, shipping_store)
    if not original:
        raise HTTPException(
            status_code=409,
            detail="Could not correlate the tracking number with a shipping draft",
        )
    replacement = _clone_replacement_draft(original, case)
    shipping_store.upsert_draft(replacement)
    updated_case = _save_case(
        damage_store,
        case_id,
        {
            "status": "replacement_prepared",
            "shipping_draft_id": original.get("id"),
            "replacement_draft_id": replacement["id"],
            "replacement_prepared_at": _now(),
        },
    )
    return {"case": updated_case, "draft": replacement, "created": True}


@router.post(
    "/damage-cases/{case_id}/create-replacement",
    summary="Create the previously prepared courier shipment",
)
def create_replacement(
    case_id: str,
    damage_store: DamageStoreDep,
    shipping_store: ShippingStoreDep,
    storage: StorageDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
) -> dict[str, Any]:
    case = _case_or_404(damage_store, case_id)
    replacement_id = case.get("replacement_draft_id")
    if not replacement_id:
        raise HTTPException(status_code=409, detail="Prepare the replacement draft first")
    draft = shipping_store.get_draft(str(replacement_id))
    if not draft:
        raise HTTPException(status_code=409, detail="Replacement draft no longer exists")
    if draft.get("status") == "created":
        updated = _save_case(
            damage_store,
            case_id,
            {
                "status": "replacement_created",
                "replacement_tracking_number": draft.get("tracking_number"),
            },
        )
        return {"case": updated, "draft": draft}
    if draft.get("status") == "needs_review":
        shipping_store.update_draft(str(replacement_id), {"status": "pending"})

    from zdrovena.api.routers.webhooks import execute_draft

    result = execute_draft(
        str(replacement_id),
        shipping_store,
        storage,
        principal,
        pickup_date=None,
        pickup_from=None,
        pickup_to=None,
    )
    draft_status = result.get("status")
    case_status = "replacement_created" if draft_status == "created" else "replacement_pending"
    updated = _save_case(
        damage_store,
        case_id,
        {
            "status": case_status,
            "replacement_tracking_number": result.get("tracking_number"),
            "replacement_created_at": _now() if draft_status == "created" else None,
        },
    )
    return {"case": updated, "draft": result}


@router.post(
    "/damage-cases/{case_id}/confirm-replacement",
    summary="Poll a pending Allegro replacement shipment",
)
def confirm_replacement(
    case_id: str,
    damage_store: DamageStoreDep,
    shipping_store: ShippingStoreDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
) -> dict[str, Any]:
    case = _case_or_404(damage_store, case_id)
    replacement_id = case.get("replacement_draft_id")
    if not replacement_id:
        raise HTTPException(status_code=409, detail="Replacement draft is missing")
    from zdrovena.api.routers.webhooks import confirm_pending_command

    result = confirm_pending_command(str(replacement_id), shipping_store, principal)
    if not isinstance(result, dict):
        response_body = getattr(result, "body", b"{}")
        result = json.loads(response_body)
    draft_status = result.get("status")
    updated = _save_case(
        damage_store,
        case_id,
        {
            "status": "replacement_created" if draft_status == "created" else "replacement_pending",
            "replacement_tracking_number": result.get("tracking_number"),
            "replacement_created_at": _now() if draft_status == "created" else None,
        },
    )
    return {"case": updated, "draft": result}


@router.post("/damage-cases/{case_id}/email-draft", summary="Prepare customer email")
def prepare_email_draft(
    case_id: str,
    damage_store: DamageStoreDep,
    shipping_store: ShippingStoreDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
) -> dict[str, Any]:
    del principal
    case = _case_or_404(damage_store, case_id)
    replacement_id = case.get("replacement_draft_id")
    draft = shipping_store.get_draft(str(replacement_id)) if replacement_id else None
    if not draft or draft.get("status") != "created":
        raise HTTPException(status_code=409, detail="Create the replacement parcel first")
    email_draft = _build_customer_email(case, draft)
    updated = _save_case(damage_store, case_id, {"email_draft": email_draft})
    return {"case": updated, "email_draft": email_draft}


@router.patch("/damage-cases/{case_id}/email-draft", summary="Edit customer email draft")
def update_email_draft(
    case_id: str,
    body: EmailDraftUpdate,
    damage_store: DamageStoreDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
) -> dict[str, Any]:
    del principal
    case = _case_or_404(damage_store, case_id)
    email_draft = case.get("email_draft")
    if not isinstance(email_draft, dict):
        raise HTTPException(status_code=409, detail="Prepare the email draft first")
    if case.get("email_sent_at"):
        raise HTTPException(status_code=409, detail="Email has already been sent")
    updated_draft = {
        **email_draft,
        "subject": body.subject,
        "body": body.body,
        "updated_at": _now(),
    }
    updated = _save_case(damage_store, case_id, {"email_draft": updated_draft})
    return {"case": updated, "email_draft": updated_draft}


@router.post("/damage-cases/{case_id}/send-email", summary="Send approved customer email")
def send_customer_email(
    case_id: str,
    damage_store: DamageStoreDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
) -> dict[str, Any]:
    case = _case_or_404(damage_store, case_id)
    email_draft = case.get("email_draft")
    if not isinstance(email_draft, dict):
        raise HTTPException(status_code=409, detail="Prepare and review the email draft first")
    if case.get("email_sent_at"):
        raise HTTPException(status_code=409, detail="Email has already been sent")
    zoho = build_zoho_client()
    if zoho is None:
        raise HTTPException(status_code=503, detail="Zoho Mail is not configured")
    allowed_senders = zoho.sender_addresses()
    if CUSTOMER_EMAIL_FROM.lower() not in allowed_senders:
        raise HTTPException(
            status_code=409,
            detail=(f"{CUSTOMER_EMAIL_FROM} is not configured as an active Zoho From address"),
        )
    if not damage_store.try_claim_email(case_id):
        raise HTTPException(
            status_code=409,
            detail="Email is already being sent or has already been sent",
        )
    try:
        response = _send_email_with_configured_zoho_smtp(
            to_address=str(email_draft["to"]),
            subject=str(email_draft["subject"]),
            content=str(email_draft["body"]),
        )
    except Exception as exc:
        logger.exception("Could not send damage-case email %s", case_id)
        _save_case(
            damage_store,
            case_id,
            {
                "email_error": str(exc),
                "email_last_attempt_at": _now(),
                "email_sending": False,
            },
        )
        raise HTTPException(status_code=502, detail="Zoho Mail could not send the message") from exc
    sent_at = _now()
    data = response.get("data") if isinstance(response, dict) else None
    message_id = data.get("messageId") if isinstance(data, dict) else None
    updated_draft = {**email_draft, "status": "sent", "sent_at": sent_at}
    updated = _save_case(
        damage_store,
        case_id,
        {
            "status": "customer_notified",
            "email_draft": updated_draft,
            "email_sent_at": sent_at,
            "email_sent_by": principal.email,
            "email_provider_message_id": message_id,
            "email_error": None,
            "email_sending": False,
        },
    )
    return {"case": updated, "email_draft": updated_draft}


@router.post("/damage-cases/{case_id}/close", summary="Close a damage case")
def close_damage_case(
    case_id: str,
    damage_store: DamageStoreDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
) -> dict[str, Any]:
    case = _case_or_404(damage_store, case_id)
    if case.get("status") not in {"replacement_created", "customer_notified"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Case is not ready to close"
        )
    return _save_case(
        damage_store,
        case_id,
        {"status": "closed", "closed_at": _now(), "closed_by": principal.email},
    )
