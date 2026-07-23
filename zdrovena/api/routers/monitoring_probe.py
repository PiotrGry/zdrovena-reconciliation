"""Bezpieczne, ukryte endpointy do testowania request telemetry na staging.

Router jest dostępny wyłącznie w nieprodukcyjnym środowisku z fake providerami
i nadal wymaga roli administratora. Pozwala potwierdzić alerty 5xx i latency bez
wywoływania logiki biznesowej ani zewnętrznych dostawców.
"""

from __future__ import annotations

import asyncio
import os
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse

from zdrovena.api.auth import Principal, require_admin
from zdrovena.api.observability import get_correlation_id
from zdrovena.common.appenv import is_production_env

router = APIRouter(tags=["monitoring-test"])


def _require_monitoring_test_support() -> None:
    fake_providers = os.getenv("PROVIDER_MODE", "").strip().lower() == "fake"
    if not fake_providers or is_production_env():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


@router.get(
    "/__test__/monitoring/request",
    include_in_schema=False,
    responses={404: {"description": "Disabled outside fake non-production mode"}},
)
async def request_telemetry_probe(
    principal: Annotated[Principal, Depends(require_admin)],
    response_status: int = Query(default=200, ge=200, le=599),
    delay_ms: int = Query(default=0, ge=0, le=5000),
) -> JSONResponse:
    """Wygeneruj kontrolowany request 2xx/5xx lub opóźnienie na staging."""

    del principal
    _require_monitoring_test_support()
    if delay_ms:
        await asyncio.sleep(delay_ms / 1000)
    if response_status >= 400:
        raise HTTPException(status_code=response_status, detail="Controlled monitoring probe")
    return JSONResponse(
        status_code=response_status,
        content={
            "status_code": response_status,
            "delay_ms": delay_ms,
            "correlation_id": get_correlation_id(),
        },
    )
