"""The dead-letter queue for drafts that failed to be created."""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Response,
    status,
)

from zdrovena.api import shipping_draft_composition as draft_composition
from zdrovena.api import shipping_execution_composition as execution_composition
from zdrovena.api.auth import Principal, require_shipment_mgr_or_above, require_viewer_or_above
from zdrovena.api.deps import ShippingStoreDep, StorageDep
from zdrovena.common.shipping_exceptions import (
    ZdrovenaShippingError,
)
from zdrovena.common.shipping_store import (
    DLQ_KIND_CREATION,
    DLQ_KIND_EXECUTION,
)

logger = logging.getLogger("zdrovena.api.routers.shipping.dlq")


router = APIRouter(tags=["shipping"])


@router.get(
    "/shipping/drafts/dlq",
    summary="List failed draft-creation attempts (DLQ)",
    responses={403: {"description": "Insufficient role"}},
)
def list_dlq(
    shipping_store: ShippingStoreDep,
    principal: Annotated[Principal, Depends(require_viewer_or_above)],
) -> dict[str, Any]:
    return {"entries": shipping_store.list_dlq()}


@router.post(
    "/shipping/drafts/dlq/{entry_id}/retry",
    summary="Retry a failed draft-creation attempt from DLQ",
    responses={
        403: {"description": "Insufficient role"},
        404: {"description": "DLQ entry not found"},
        502: {"description": "Retry failed — entry left in DLQ with updated error"},
    },
)
def retry_dlq_entry(
    entry_id: str,
    shipping_store: ShippingStoreDep,
    storage: StorageDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
) -> dict[str, Any]:
    entry = shipping_store.get_dlq_entry(entry_id)
    if not entry:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="DLQ entry not found")
    payload = entry.get("payload") or {}
    source = entry.get("source") or "shopify"
    # Entries written before `kind` existed were all creations.
    kind = entry.get("kind") or DLQ_KIND_CREATION
    try:
        if kind == DLQ_KIND_EXECUTION:
            # The draft already exists — re-run the courier call, never the
            # ingestion, which would duplicate it. Same role guards both
            # endpoints, so reusing the principal grants nothing extra.
            target_draft_id = entry.get("draft_id")
            if not target_draft_id:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="DLQ entry has kind=draft_execution but no draft_id",
                )
            try:
                execution_composition.execute_shipping_draft(
                    target_draft_id,
                    shipping_store,
                    storage,
                    failure_dlq_entry_id=entry_id,
                )
            except execution_composition.EXECUTION_APPLICATION_HTTP_ERRORS as exc:
                execution_composition.raise_execution_http_exception(exc)
        else:
            draft_composition.create_draft(payload, shipping_store, source=source)
    except HTTPException:
        raise
    except ZdrovenaShippingError as exc:
        # Execution retries update their original DLQ entry inside
        # the application workflow before the domain exception is re-raised. Do not
        # increment the same entry a second time here.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Retry failed: {type(exc).__name__}: {exc}",
        ) from exc
    except Exception as exc:
        logger.exception("DLQ retry failed for entry %s: %s", entry_id, exc)
        # bump retries + last_error; keep the entry in DLQ
        try:
            shipping_store.enqueue_dlq(
                payload=payload,
                error=f"{type(exc).__name__}: {exc}",
                source=source,
                entry_id=entry_id,
            )
        except Exception:
            logger.exception("DLQ update after retry failure failed for %s", entry_id)
        # DLQ retry to endpoint diagnostyczny operatora — surowy błąd upstream
        # jest tu celowo zwracany, żeby operator mógł zdecydować o dalszej akcji.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Retry failed: {type(exc).__name__}: {exc}",
        ) from exc
    # success → remove from DLQ
    shipping_store.delete_dlq_entry(entry_id)
    return {"status": "retried", "entry_id": entry_id}


@router.delete(
    "/shipping/drafts/dlq/{entry_id}",
    summary="Discard a DLQ entry without retrying",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        403: {"description": "Insufficient role"},
        404: {"description": "DLQ entry not found"},
    },
)
def delete_dlq_entry(
    entry_id: str,
    shipping_store: ShippingStoreDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
) -> Response:
    entry = shipping_store.get_dlq_entry(entry_id)
    if not entry:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="DLQ entry not found")
    shipping_store.delete_dlq_entry(entry_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
