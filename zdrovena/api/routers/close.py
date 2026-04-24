"""POST /close — trigger monthly close pipeline."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from zdrovena.api.auth import Principal, require_accountant_or_admin
from zdrovena.api.models import CloseRequest, CloseResponse, CloseStateResponse
from zdrovena.common.storage import get_storage_service
from zdrovena.month_closing.config import BASE_DIR, POLISH_MONTHS
from zdrovena.month_closing.orchestrator import MonthCloseOrchestrator
from zdrovena.month_closing.state import PipelineState

logger = logging.getLogger("zdrovena.api.routers.close")
router = APIRouter(prefix="/close", tags=["close"])


@router.post(
    "",
    response_model=CloseResponse,
    summary="Run monthly close pipeline",
    status_code=status.HTTP_200_OK,
    responses={
        400: {"description": "Invalid month/year"},
        403: {"description": "Insufficient role"},
        500: {"description": "Pipeline error"},
    },
)
def run_close(
    req: CloseRequest,
    principal: Annotated[Principal, Depends(require_accountant_or_admin)],
) -> CloseResponse:
    """Run the month-close pipeline for the given year/month.

    Requires ``zdrovena-accountant`` or ``zdrovena-admin`` role.
    Use ``dry_run=true`` to simulate without writing files or sending email.
    """
    logger.info(
        "Close requested by %s: %d/%02d dry_run=%s",
        principal.email,
        req.year,
        req.month,
        req.dry_run,
    )
    try:
        orchestrator = MonthCloseOrchestrator(
            year=req.year,
            month=req.month,
            dry_run=req.dry_run,
            non_interactive=True,
            ignore_warnings=req.ignore_warnings,
            ignore_vendors=req.ignore_vendors,
        )
        report = orchestrator.execute()
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Close pipeline failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Pipeline error — check server logs",
        ) from exc

    return CloseResponse.from_close_report(report)


@router.get(
    "/state",
    response_model=CloseStateResponse,
    summary="Get pipeline checkpoint state for a given month",
)
def get_close_state(
    year: int = Query(...),
    month: int = Query(..., ge=1, le=12),
    principal: Annotated[Principal, Depends(require_accountant_or_admin)] = None,
) -> CloseStateResponse:
    """Return which pipeline steps have already been completed for the given month."""
    month_pl = POLISH_MONTHS[month - 1]
    month_dir = BASE_DIR / str(year) / month_pl
    storage = get_storage_service()
    blob_key = f"faktury/{year}/{month_pl}/.state.json"
    state = PipelineState(month_dir, storage=storage, blob_key=blob_key)
    return CloseStateResponse(completed_steps=state.completed_steps)
