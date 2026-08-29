"""Label PDFs, single and batched."""

from __future__ import annotations

import io
import logging
import re
import unicodedata
from datetime import datetime
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Body,
    Depends,
    HTTPException,
    Query,
)
from fastapi.responses import StreamingResponse

from zdrovena.api import shipping_execution_composition as execution_composition
from zdrovena.api.auth import Principal, require_viewer_or_above
from zdrovena.api.deps import ShippingStoreDep, StorageDep
from zdrovena.api.routers.shipping import deps
from zdrovena.common.shipping_exceptions import (
    AllegroAuthError,
    AllegroBusinessError,
    CourierTransientError,
    InPostBusinessError,
    LabelNotReadyError,
    ZdrovenaShippingError,
)
from zdrovena.shipping.domain.labels import WARSAW, batch_label_title, single_label_title

logger = logging.getLogger("zdrovena.api.routers.shipping.labels")

router = APIRouter(tags=["shipping"])


_SUPPORTED_LABEL_COURIERS = ("inpost", "apaczka", "allegro_delivery")


_MAX_BATCH_LABELS = 100  # provider-agnostic safety cap on one batch print


def _now_warsaw() -> datetime:
    """Single seam so tests can freeze the day used in label titles."""
    return datetime.now(WARSAW)


def _label_filename(title: str) -> str:
    """Return an ASCII-only filename safe for a quoted response header."""
    normalized = unicodedata.normalize("NFKD", title)
    ascii_title = normalized.encode("ascii", "ignore").decode("ascii")
    safe = re.sub(r"[^A-Za-z0-9 ._-]+", "_", ascii_title).strip(" ._-")
    return f"{safe[:80] or 'label'}.pdf"


def _fetch_label_pdfs(draft: dict[str, Any], courier: str, storage: Any) -> list[bytes]:
    """Fetch every label PDF for a draft. Shared by the single-label and batch
    endpoints (R5-B).

    Raises :class:`LabelNotReadyError` (HTTP 409) when the label is not printable
    yet — either the draft has no courier id, or InPost rejects the fetch with a
    business error (almost always "shipment not confirmed/processed yet"). Other
    courier failures surface as HTTP 502.
    """
    if courier == "allegro_delivery":
        label_ids = execution_composition.dispatch_shipment_ids(draft)
    else:
        label_ids = [shipment.get("id") for shipment in draft.get("courier_shipments") or []]
        if not label_ids:
            label_ids = [draft.get("courier_draft_id")]
    label_ids = [str(label_id) for label_id in label_ids if label_id]
    if not label_ids:
        raise HTTPException(status_code=404, detail="No courier draft ID — draft may have failed")

    try:
        if courier == "inpost":
            from zdrovena.common.inpost import InPostClient

            token = deps.get_secret("inpost_api_token")
            org_id = deps.get_secret("inpost_organization_id")
            try:
                pdfs = [InPostClient(token, org_id).get_label(label_id) for label_id in label_ids]
                return pdfs
            except InPostBusinessError as exc:
                # A business rejection while fetching a label means the shipment
                # is not confirmed/processed yet → not ready, not a hard failure.
                raise LabelNotReadyError(str(exc), courier="inpost", action="get_label") from exc
        elif courier == "apaczka":
            from zdrovena.common.apaczka import ApaczkaClient

            app_id = deps.get_secret("apaczka_app_id")
            app_secret = deps.get_secret("apaczka_app_secret")
            service_id = draft.get("apaczka_service_id") or ""
            client = ApaczkaClient(app_id, app_secret, service_id, storage)
            pdfs = [client.get_label(label_id) for label_id in label_ids]
            return pdfs
        else:  # allegro_delivery
            client = execution_composition.get_allegro_client()
            if client is None:
                raise HTTPException(status_code=502, detail="Allegro credentials missing")
            try:
                pdfs = [client.get_ship_with_allegro_label(label_id) for label_id in label_ids]
                return pdfs
            except (AllegroBusinessError, AllegroAuthError, CourierTransientError) as exc:
                logger.exception("Allegro label fetch failed for draft %s", draft.get("id"))
                raise HTTPException(status_code=502, detail=f"Allegro API error: {exc}") from exc
    except (HTTPException, ZdrovenaShippingError):
        raise
    except Exception as exc:
        logger.exception("Label fetch failed for draft %s", draft.get("id"))
        raise HTTPException(status_code=502, detail=f"Courier API error: {exc}") from exc


def _titled_pdf(pdfs: list[bytes], title: str) -> bytes:
    """Assemble label PDFs into one document carrying ``title`` as its /Title.

    Chrome takes the "Save as PDF" filename from the printed document's title.
    The label is printed from a ``blob:`` URL, which has no filename and does
    not carry Content-Disposition, so this metadata is the only lever we have.

    A single carrier PDF pypdf cannot parse is returned unchanged: an
    unprintable label is a worse failure than an untitled one. A multi-PDF
    merge still raises, because there is no meaningful fallback for it and
    that was already the behaviour.
    """
    from pypdf import PdfWriter

    try:
        writer = PdfWriter()
        for pdf in pdfs:
            writer.append(io.BytesIO(pdf))
        writer.add_metadata({"/Title": title})
        out = io.BytesIO()
        writer.write(out)
        writer.close()
        return out.getvalue()
    except Exception:
        if len(pdfs) != 1:
            raise
        logger.exception("Could not title a label PDF — streaming it untitled")
        return pdfs[0]


@router.post(
    "/shipping/labels/batch",
    summary="Fetch and merge labels for several drafts into one printable PDF",
    responses={
        400: {"description": "No draft_ids, too many, or unsupported courier"},
        404: {"description": "None of the drafts exist"},
        409: {"description": "One or more labels are not ready yet"},
    },
)
def batch_labels(
    shipping_store: ShippingStoreDep,
    storage: StorageDep,
    principal: Annotated[Principal, Depends(require_viewer_or_above)],
    draft_ids: Annotated[list[str], Body(embed=True)],
) -> StreamingResponse:
    """Merge the labels of the given drafts into one PDF (R5-B).

    Drafts are grouped by courier (each fetched via the same path as the single
    label endpoint), then concatenated in the request order. Fails deterministically:
      * empty / oversized ``draft_ids`` → 400
      * a not-yet-ready label → 409 listing the offending drafts
      * an unknown draft id → 404 listing them
    """
    if not draft_ids:
        raise HTTPException(status_code=400, detail="draft_ids must not be empty")
    if len(draft_ids) > _MAX_BATCH_LABELS:
        raise HTTPException(
            status_code=400,
            detail=f"Too many labels in one batch (max {_MAX_BATCH_LABELS}, got {len(draft_ids)})",
        )

    drafts: list[dict[str, Any]] = []
    missing: list[str] = []
    for did in draft_ids:
        d = shipping_store.get_draft(did)
        if d is None:
            missing.append(did)
        else:
            drafts.append(d)
    if missing:
        raise HTTPException(status_code=404, detail=f"Draft(s) not found: {', '.join(missing)}")

    bad_courier = [d.get("id") for d in drafts if d.get("courier") not in _SUPPORTED_LABEL_COURIERS]
    if bad_courier:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported courier for draft(s): {', '.join(map(str, bad_courier))}",
        )

    # Group by courier so a future provider bulk-label API can be slotted in per
    # group; today we fetch each label and merge. Order within the response
    # follows the original draft_ids order for predictable printing.
    pdfs: list[bytes] = []
    not_ready: list[str] = []
    for d in drafts:
        try:
            pdfs.extend(_fetch_label_pdfs(d, d["courier"], storage))
        except LabelNotReadyError:
            not_ready.append(str(d.get("id")))
        except HTTPException as exc:
            # A missing courier id (404) means the draft exists but has no label
            # yet — for a batch that is just another "not ready" case, not a hard
            # failure. Any other courier error (502) aborts the whole batch.
            if exc.status_code == 404:
                not_ready.append(str(d.get("id")))
            else:
                raise
    if not_ready:
        raise HTTPException(
            status_code=409,
            detail=f"Etykiety nie są jeszcze gotowe dla: {', '.join(not_ready)}",
        )

    title = batch_label_title(_now_warsaw())
    return StreamingResponse(
        io.BytesIO(_titled_pdf(pdfs, title)),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{_label_filename(title)}"'},
    )


@router.get(
    "/shipping/drafts/{draft_id}/label",
    summary="Stream shipping label PDF",
    responses={403: {"description": "Insufficient role"}, 404: {"description": "Draft not found"}},
)
def get_label(
    draft_id: str,
    shipping_store: ShippingStoreDep,
    storage: StorageDep,
    principal: Annotated[Principal, Depends(require_viewer_or_above)],
    courier: str = Query(
        None, description="inpost, apaczka, or allegro_delivery (defaults to draft's courier)"
    ),
) -> StreamingResponse:
    draft = shipping_store.get_draft(draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")

    # Prefer the stored draft courier over the query param (prevents mismatch)
    courier = draft.get("courier") or courier
    _SUPPORTED_COURIERS = ("inpost", "apaczka", "allegro_delivery")
    if courier not in _SUPPORTED_COURIERS:
        raise HTTPException(
            status_code=400,
            detail=f"courier must be one of: {', '.join(_SUPPORTED_COURIERS)}",
        )

    pdfs = _fetch_label_pdfs(draft, courier, storage)
    title = single_label_title(str(draft.get("shopify_order_number") or ""), _now_warsaw())
    return StreamingResponse(
        io.BytesIO(_titled_pdf(pdfs, title)),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{_label_filename(title)}"'},
    )
