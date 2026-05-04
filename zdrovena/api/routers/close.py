"""POST /close — trigger monthly close pipeline."""

from __future__ import annotations

import logging
from io import StringIO
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from zdrovena.api.auth import Principal, require_accountant_or_admin, require_viewer_or_above
from zdrovena.api.models import CloseRequest, CloseResponse, CloseStateResponse
from zdrovena.common.storage import get_storage_service
from zdrovena.month_closing.config import BASE_DIR, POLISH_MONTHS
from zdrovena.month_closing.console import ConsoleReporter
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
        buf = StringIO()
        orchestrator = MonthCloseOrchestrator(
            year=req.year,
            month=req.month,
            dry_run=req.dry_run,
            non_interactive=True,
            ignore_warnings=req.ignore_warnings,
            ignore_vendors=req.ignore_vendors,
        )
        orchestrator.out = ConsoleReporter(stream=buf)
        report = orchestrator.execute()
        log_lines = buf.getvalue().splitlines()
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except SystemExit:
        # Pre-flight found blockers (missing files/invoices) — treat as 422, not 500.
        # report.errors was populated by the orchestrator before raising SystemExit.
        log_lines = buf.getvalue().splitlines()
        blockers = orchestrator.report.errors
        detail = blockers if blockers else ["Pre-flight checks failed — check server logs"]
        logger.warning("Close pre-flight blocked for %d/%02d: %s", req.year, req.month, detail)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"blockers": detail, "log_lines": log_lines},
        )
    except Exception as exc:
        logger.exception("Close pipeline failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Pipeline error — check server logs",
        ) from exc

    return CloseResponse.from_close_report(report, log_lines=log_lines)


@router.get(
    "/state",
    response_model=CloseStateResponse,
    summary="Get pipeline checkpoint state for a given month",
)
def get_close_state(
    principal: Annotated[Principal, Depends(require_viewer_or_above)],
    year: int = Query(...),
    month: int = Query(..., ge=1, le=12),
) -> CloseStateResponse:
    """Return which pipeline steps have already been completed for the given month.

    Read-only — accessible to viewer role and above (D3 decision from eng review).
    """
    month_pl = POLISH_MONTHS[month - 1]
    month_dir = BASE_DIR / str(year) / month_pl
    storage = get_storage_service()
    blob_key = f"faktury/{year}/{month_pl}/.state.json"
    state = PipelineState(month_dir, storage=storage, blob_key=blob_key)
    return CloseStateResponse(completed_steps=state.completed_steps)
