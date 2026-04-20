"""POST /close — trigger monthly close pipeline."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from zdrovena.api.auth import Principal, require_accountant_or_admin
from zdrovena.api.models import CloseRequest, CloseResponse
from zdrovena.month_closing.orchestrator import MonthCloseOrchestrator

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
