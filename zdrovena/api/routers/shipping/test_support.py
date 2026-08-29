"""Endpoints that exist only for E2E runs. Fail closed in production."""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Body,
    Depends,
    HTTPException,
    status,
)

from zdrovena.api.auth import Principal, require_shipment_mgr_or_above
from zdrovena.api.deps import ShippingStoreDep
from zdrovena.api.routers.shipping import deps
from zdrovena.common.events import log_event

logger = logging.getLogger("zdrovena.api.routers.shipping.test_support")

router = APIRouter(tags=["shipping"])


@router.post(
    "/__test__/shipping/reset",
    include_in_schema=False,
    responses={404: {"description": "Disabled outside fake non-production mode"}},
)
def reset_e2e_shipping_state(
    shipping_store: ShippingStoreDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
) -> dict[str, int]:
    deps._require_test_support()
    removed_drafts = 0
    for draft in shipping_store.list_drafts(limit=200):
        if deps._is_e2e_record(draft):
            shipping_store.delete_draft(str(draft["id"]))
            removed_drafts += 1

    removed_dlq = 0
    for entry in shipping_store.list_dlq(limit=200):
        if deps._is_e2e_record(entry):
            shipping_store.delete_dlq_entry(str(entry["id"]))
            removed_dlq += 1

    return {"removed_drafts": removed_drafts, "removed_dlq": removed_dlq}


@router.post(
    "/__test__/shipping/drafts",
    include_in_schema=False,
    responses={404: {"description": "Disabled outside fake non-production mode"}},
)
def seed_e2e_shipping_draft(
    draft: Annotated[dict[str, Any], Body()],
    shipping_store: ShippingStoreDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
) -> dict[str, Any]:
    deps._require_test_support()
    if not deps._is_e2e_record(draft):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="E2E draft id must start with e2e- or order number with 990",
        )
    if not draft.get("id"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Draft id required")
    shipping_store.upsert_draft(draft)
    return shipping_store.get_draft(str(draft["id"])) or draft


@router.post(
    "/__test__/shipping/dlq",
    include_in_schema=False,
    responses={404: {"description": "Disabled outside fake non-production mode"}},
)
def seed_e2e_dlq_entry(
    body: Annotated[dict[str, Any], Body()],
    shipping_store: ShippingStoreDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
) -> dict[str, Any]:
    deps._require_test_support()
    payload = body.get("payload") or {}
    entry_id = str(body.get("id") or "")
    probe = {"id": entry_id, "payload": payload}
    if not deps._is_e2e_record(probe):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="E2E DLQ id must start with e2e- or payload order number with 990",
        )
    entry = shipping_store.enqueue_dlq(
        payload=payload,
        error=str(body.get("error") or "E2E seeded failure"),
        source=str(body.get("source") or "shopify"),
        entry_id=entry_id or None,
    )
    log_event(
        "dlq.enqueued",
        level=logging.ERROR,
        entry_id=entry["id"],
        order_number=payload.get("order_number") or payload.get("id"),
        source=entry["source"],
        error_type="E2ESeededFailure",
        test_probe=True,
    )
    return entry
