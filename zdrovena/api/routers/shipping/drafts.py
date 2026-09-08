"""Reading and editing shipping drafts."""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Body,
    Depends,
    HTTPException,
)

from zdrovena.api.auth import Principal, require_shipment_mgr_or_above, require_viewer_or_above
from zdrovena.api.deps import ShippingStoreDep
from zdrovena.api.models import (
    ApaczkaServicesResponse,
    ShippingDraftModel,
    ShippingDraftsResponse,
)
from zdrovena.common.shipping_format import normalize_pl_phone
from zdrovena.shipping.domain.cod import CodAllocationError, cod_allocation
from zdrovena.shipping.domain.planning import physical_parcels

logger = logging.getLogger("zdrovena.api.routers.shipping.drafts")


router = APIRouter(tags=["shipping"])


_MATCH_MANUAL = "manual"


def _with_cod_split(draft: dict[str, Any]) -> dict[str, Any]:
    """Annotate a multi-parcel COD draft with what each parcel collects.

    Computed on read rather than stored, for the same reason the providers
    compute it: a stored copy would disagree with the plan the moment the
    operator repacked. A draft whose split is impossible keeps its reason and
    stays in the list — a listing that fails closed hides every other order too.
    """
    if not draft.get("cod") or len(physical_parcels(draft)) < 2:
        return draft
    try:
        allocation = cod_allocation(draft)
    except CodAllocationError as exc:
        return {**draft, "cod_split_error": str(exc)}
    return {
        **draft,
        "cod_split": [str(amount) for amount in allocation.amounts],
        "cod_split_basis": allocation.basis,
    }


@router.get(
    "/shipping/drafts",
    summary="List shipping drafts",
    responses={403: {"description": "Insufficient role"}},
    response_model=ShippingDraftsResponse,
    response_model_exclude_unset=True,
)
def list_drafts(
    shipping_store: ShippingStoreDep,
    principal: Annotated[Principal, Depends(require_viewer_or_above)],
) -> dict[str, Any]:
    drafts = [_with_cod_split(draft) for draft in shipping_store.list_drafts()]
    return {"drafts": drafts}


@router.get(
    "/shipping/apaczka-services",
    summary="List the curated Apaczka courier services available for draft selection",
    responses={403: {"description": "Insufficient role"}},
    response_model=ApaczkaServicesResponse,
    response_model_exclude_unset=True,
)
def list_apaczka_services(
    principal: Annotated[Principal, Depends(require_viewer_or_above)],
) -> dict[str, Any]:
    from zdrovena.common.apaczka import APACZKA_SERVICE_CATALOG

    return {
        "services": [
            {"service_id": service_id, "label": label}
            for service_id, label in APACZKA_SERVICE_CATALOG.items()
        ]
    }


_MAX_BREAKDOWN_ROWS = 20


_MAX_TOTAL_PARCELS = 30


# Past these statuses the parcel plan describes shipments that already exist at
# the carrier. Editing it would make the record disagree with the printed labels.
_BREAKDOWN_LOCKED_STATUSES = frozenset(
    {"executing", "pending_confirmation", "created", "cancelled"}
)


def _breakdown_locked_reason(draft: dict[str, Any]) -> str | None:
    """Say why this draft's parcel plan may no longer be edited, or None.

    Status is the usual answer. The exception is a draft that failed halfway:
    it lands in "error", which is editable on purpose — most failures happen
    before anything is booked, and repacking is the operator's way out of them.
    But the parcels created before the failure are printed and paid for, and
    the plan is what numbers them: repacking would renumber the ones still to
    come ("1/2" already at the carrier, "2/3" booked next) and changing a type
    would strand the created label on a box no longer in the plan. So a label
    at the carrier freezes the plan whatever the status says.
    """
    if draft.get("status") in _BREAKDOWN_LOCKED_STATUSES:
        return "Nie można zmienić paczek po wysłaniu przesyłki do kuriera"
    if draft.get("courier_shipments"):
        return (
            "Nie można zmienić paczek — część etykiet jest już u kuriera. "
            "Dokończ wysyłkę albo anuluj draft."
        )
    return None


def _validated_breakdown(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalise an operator parcel plan or raise a 400 the operator can read."""
    from zdrovena.common.shipping_parcels import PARCEL_SPECS

    if not rows:
        raise HTTPException(status_code=400, detail="Plan paczek nie może być pusty")
    if len(rows) > _MAX_BREAKDOWN_ROWS:
        raise HTTPException(
            status_code=400,
            detail=f"Za dużo pozycji w planie paczek (maksymalnie {_MAX_BREAKDOWN_ROWS})",
        )

    cleaned: list[dict[str, Any]] = []
    for row in rows:
        package_type = str(row.get("type") or "").strip()
        if package_type not in PARCEL_SPECS:
            raise HTTPException(status_code=400, detail=f"Nieznany typ paczki: {package_type}")
        raw_qty = row.get("qty")
        if raw_qty is None:
            raise HTTPException(
                status_code=400,
                detail=f"Liczba sztuk dla {package_type} musi być liczbą całkowitą",
            )
        try:
            qty = int(raw_qty)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400,
                detail=f"Liczba sztuk dla {package_type} musi być liczbą całkowitą",
            ) from None
        if not 1 <= qty <= 99:
            raise HTTPException(
                status_code=400,
                detail=f"Liczba sztuk dla {package_type} musi mieścić się w zakresie 1–99",
            )
        cleaned.append({"type": package_type, "qty": qty})

    total = sum(row["qty"] for row in cleaned)
    if total > _MAX_TOTAL_PARCELS:
        raise HTTPException(
            status_code=400,
            detail=f"Za dużo paczek w jednym zamówieniu ({total}, maksymalnie {_MAX_TOTAL_PARCELS})",
        )
    return cleaned


@router.patch(
    "/shipping/drafts/{draft_id}",
    summary="Update draft metadata (packages_breakdown, service, locker_id, receiver_phone)",
    responses={
        403: {"description": "Insufficient role"},
        404: {"description": "Draft not found"},
        400: {"description": "Invalid service for courier"},
    },
    response_model=ShippingDraftModel,
    response_model_exclude_unset=True,
)
def update_draft(
    draft_id: str,
    shipping_store: ShippingStoreDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
    packages_count: int | None = Body(None, ge=1, le=99),
    # Body(None) is FastAPI's documented way to declare an optional list-typed
    # body field. Ruff's bugbear check flags list/dict-annotated Body(...)
    # defaults as if they were mutable-default literals, but the actual
    # default is an immutable FieldInfo sentinel, not a list.
    packages_breakdown: list[dict[str, Any]] | None = Body(None),  # noqa: B008
    service: str | None = Body(None),
    locker_id: str | None = Body(None),
    receiver_phone: str | None = Body(None),
    apaczka_service_id: str | None = Body(None),
    reviewed: bool | None = Body(None),
) -> dict[str, Any]:
    draft = shipping_store.get_draft(draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")

    patch: dict[str, Any] = {}
    if packages_count is not None:
        patch["packages_count"] = packages_count
    if packages_breakdown is not None:
        if packages_count is not None:
            raise HTTPException(
                status_code=400,
                detail="Podaj plan paczek albo liczbę paczek, nie oba naraz",
            )
        locked_reason = _breakdown_locked_reason(draft)
        if locked_reason:
            raise HTTPException(status_code=409, detail=locked_reason)
        cleaned = _validated_breakdown(packages_breakdown)
        total = sum(row["qty"] for row in cleaned)
        if draft.get("cod"):
            if draft.get("service") == "inpost_locker_standard" and total != 1:
                # A locker is collected parcel by parcel, so a split would let
                # the customer pay for one box and abandon the rest.
                raise HTTPException(
                    status_code=400,
                    detail="Pobranie do paczkomatu musi mieścić się w jednej paczce",
                )
            try:
                # Checked against the plan being saved, not the stored one, so
                # an impossible split is refused here rather than at execute
                # time when the operator is waiting on labels.
                cod_allocation({**draft, "packages_breakdown": cleaned})
            except CodAllocationError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"Nie można podzielić pobrania na te paczki: {exc}",
                ) from exc
        logger.info(
            "Operator repacked draft %s: %s -> %s",
            draft_id,
            draft.get("packages_breakdown"),
            cleaned,
        )
        patch["packages_breakdown"] = cleaned
        patch["packages_count"] = total
        patch["packages_source"] = "operator"
    if service is not None:
        valid = {"inpost_locker_standard", "inpost_courier_standard", "apaczka"}
        if service not in valid:
            raise HTTPException(status_code=400, detail=f"Unknown service: {service}")
        if draft.get("courier") == "inpost" and service == "apaczka":
            raise HTTPException(status_code=400, detail="Cannot switch InPost draft to apaczka")
        patch["service"] = service
        patch["shipping_service_match_status"] = _MATCH_MANUAL
        patch["shipping_service_match_source"] = "operator"
        patch["shipping_service_match_detail"] = "Manual service override"
    # Both edits below live on the receiver. Built once and shared, because two
    # branches each assigning patch["receiver"] would silently drop the first.
    receiver = dict(draft.get("receiver") or {})
    receiver_changed = False
    if locker_id is not None:
        receiver["locker_id"] = locker_id
        receiver_changed = True
    if receiver_phone is not None:
        normalized_phone = normalize_pl_phone(receiver_phone)
        if not normalized_phone:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Numer telefonu nie jest poprawnym polskim numerem "
                    "(oczekiwane 9 cyfr lub +48 i 9 cyfr)"
                ),
            )
        receiver["phone"] = normalized_phone
        receiver_changed = True
    if receiver_changed:
        patch["receiver"] = receiver
    if apaczka_service_id is not None:
        from zdrovena.common.apaczka import APACZKA_SERVICE_CATALOG

        if apaczka_service_id not in APACZKA_SERVICE_CATALOG:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown apaczka_service_id: {apaczka_service_id}",
            )
        patch["apaczka_service_id"] = apaczka_service_id
        patch["shipping_service_match_status"] = _MATCH_MANUAL
        patch["shipping_service_match_source"] = "operator"
        patch["shipping_service_match_detail"] = "Manual Apaczka service override"
    if reviewed is True and draft.get("status") == "needs_review":
        # Read the phone from the patch first, falling back to the stored draft:
        # an operator supplying the number and clearing review in one request
        # must succeed. Without this guard a single click made a phone-less
        # InPost draft executable, and merge_synced_draft then kept it that way.
        effective_receiver = patch.get("receiver") or draft.get("receiver") or {}
        if draft.get("courier") == "inpost" and not normalize_pl_phone(
            effective_receiver.get("phone")
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    "InPost wymaga numeru telefonu odbiorcy — uzupełnij go "
                    "przed zatwierdzeniem draftu"
                ),
            )
        patch["status"] = "pending"
        patch["error"] = None

    if patch:
        shipping_store.update_draft(draft_id, patch)
    updated = shipping_store.get_draft(draft_id)
    return updated or {"draft_id": draft_id}
