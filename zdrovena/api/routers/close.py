"""POST /close — trigger monthly close pipeline."""

from __future__ import annotations

import logging
from io import StringIO
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from zdrovena.api.auth import Principal, require_accountant_or_admin, require_viewer_or_above
from zdrovena.api.models import CloseRequest, CloseResponse, CloseStateResponse
from zdrovena.common.storage import get_storage_service
from zdrovena.month_closing.close_history import append_close_history, build_history_entry, delete_history_entry, read_close_history
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
        log_lines = buf.getvalue().splitlines()
        blockers = orchestrator.report.errors
        detail = blockers if blockers else ["Pre-flight checks failed — check server logs"]
        logger.warning("Close pre-flight blocked for %d/%02d: %s", req.year, req.month, detail)
        append_close_history(get_storage_service(), build_history_entry(
            year=req.year, month=req.month, month_name=POLISH_MONTHS[req.month],
            status="blocked", dry_run=req.dry_run,
            report=orchestrator.report, error="; ".join(detail),
        ))
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"blockers": detail, "log_lines": log_lines},
        ) from None
    except RuntimeError as exc:
        log_lines = buf.getvalue().splitlines()
        logger.warning("Close pipeline blocked for %d/%02d: %s", req.year, req.month, exc)
        append_close_history(get_storage_service(), build_history_entry(
            year=req.year, month=req.month, month_name=POLISH_MONTHS[req.month],
            status="error", dry_run=req.dry_run,
            report=orchestrator.report, error=str(exc),
        ))
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"blockers": [str(exc)], "log_lines": log_lines},
        ) from exc
    except Exception as exc:
        logger.exception("Close pipeline failed: %s", exc)
        log_lines = buf.getvalue().splitlines()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"blockers": [f"Nieoczekiwany błąd: {exc}"], "log_lines": log_lines},
        ) from exc

    append_close_history(orchestrator.storage, build_history_entry(
        year=req.year, month=req.month, month_name=POLISH_MONTHS[req.month],
        status="success" if not report.warnings else "partial",
        dry_run=req.dry_run, report=report,
    ))
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


@router.get(
    "/history",
    summary="Get history of month-closing runs",
)
def get_close_history(
    principal: Annotated[Principal, Depends(require_viewer_or_above)],
    limit: int = Query(default=20, ge=1, le=100),
) -> list[dict]:
    """Return last N close runs, newest first. Accessible to viewer role and above."""
    storage = get_storage_service()
    return read_close_history(storage, limit=limit)


@router.delete(
    "/history/{ts:path}",
    summary="Delete a history entry by timestamp",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_close_history_entry(
    ts: str,
    principal: Annotated[Principal, Depends(require_accountant_or_admin)],
) -> None:
    """Remove one history entry. Requires accountant or admin role."""
    storage = get_storage_service()
    if not delete_history_entry(storage, ts):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Entry not found")
