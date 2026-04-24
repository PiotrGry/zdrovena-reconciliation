"""
zdrovena.api.main – FastAPI application entry-point
"""

from __future__ import annotations

import logging
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from zdrovena.api.routers import close, files, invoices

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

app = FastAPI(
    title="Zdrovena Reconciliation API",
    version="2.0.0",
    description=(
        "REST API for the Zdrovena monthly accounting close pipeline. "
        "All endpoints require a valid Azure Entra ID JWT (Bearer token). "
        "Set AZURE_AUTH_DISABLED=true for local dev."
    ),
    docs_url="/docs",
    redoc_url="/redoc",
    redirect_slashes=False,
)

# CORS — restrict to known origins in production via ALLOWED_ORIGINS env var
_origins = [
    o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "http://localhost:5173").split(",")
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)

app.include_router(close.router)
app.include_router(files.router)
app.include_router(invoices.router)


@app.get("/health", tags=["health"])
def health() -> dict:
    return {"status": "ok", "version": app.version}
