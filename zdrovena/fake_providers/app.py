"""Compose independent, contract-first HTTP provider emulators.

Each provider router owns its validation, authentication and lifecycle.  The
control endpoints below only reset/inspect state and select deterministic test
scenarios; they do not redefine provider behavior.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from zdrovena.fake_providers.allegro import router as allegro_router
from zdrovena.fake_providers.apaczka import router as apaczka_router
from zdrovena.fake_providers.fakturownia import router as fakturownia_router
from zdrovena.fake_providers.inpost import router as inpost_router
from zdrovena.fake_providers.state import STATE

app = FastAPI(title="Zdrovena provider emulators", version="2.0.0")
app.include_router(allegro_router)
app.include_router(inpost_router)
app.include_router(apaczka_router)
app.include_router(fakturownia_router)


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "providers": ["allegro", "inpost", "apaczka", "fakturownia"],
        "contract_mode": "provider-first",
    }


@app.post("/__fake__/reset")
def reset() -> dict[str, str]:
    STATE.reset()
    return {"status": "reset"}


@app.get("/__fake__/state")
def state() -> dict[str, Any]:
    return {
        "scenarios": dict(STATE.scenarios),
        "allegro": {
            "orders": deepcopy(STATE.allegro_orders),
            "commands": deepcopy(STATE.allegro_commands),
            "pickupCommands": deepcopy(STATE.allegro_pickup_commands),
            "shipments": deepcopy(STATE.allegro_shipments),
            "invoices": deepcopy(STATE.allegro_invoices),
            "dispatches": deepcopy(STATE.allegro_dispatches),
        },
        "inpost": {
            "shipments": deepcopy(STATE.inpost_shipments),
            "dispatches": deepcopy(STATE.inpost_dispatches),
        },
        "apaczka": {"orders": deepcopy(STATE.apaczka_orders)},
        "fakturownia": {"invoices": deepcopy(STATE.fakturownia_invoices)},
    }


@app.post("/__fake__/scenario")
async def scenario(request: Request) -> dict[str, str]:
    body = await request.json()
    provider = str(body.get("provider") or "")
    operation = str(body.get("operation") or "")
    mode = str(body.get("mode") or "")
    if not provider or not operation or not mode:
        raise HTTPException(status_code=422, detail="provider, operation and mode are required")
    key = f"{provider}:{operation}"
    STATE.scenarios[key] = mode
    return {"status": "ok", "key": key, "mode": mode}


@app.exception_handler(HTTPException)
async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def unsupported(path: str) -> PlainTextResponse:
    return PlainTextResponse(f"Unsupported provider emulator route: /{path}", status_code=404)
