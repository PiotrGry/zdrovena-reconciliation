"""
zdrovena.api.main – FastAPI application entry-point
"""

from __future__ import annotations

import logging
import os
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from zdrovena.api.routers import close, files, invoices, webhooks

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

logger = logging.getLogger("zdrovena.api.main")

# Configure Azure Monitor before app = FastAPI() so the FastAPI instrumentor
# patches FastAPI.__init__ before our app instance is created.
# Per official sample: https://github.com/Azure/azure-sdk-for-python/blob/main/
#   sdk/monitor/azure-monitor-opentelemetry/samples/tracing/http_fastapi.py
if os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING"):
    try:
        from azure.monitor.opentelemetry import configure_azure_monitor

        configure_azure_monitor()
        logger.info("Azure Monitor OpenTelemetry configured.")
    except Exception as exc:
        logger.warning("Azure Monitor configuration failed (non-fatal): %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[type-arg]
    """Startup: verify Key Vault is reachable before accepting traffic.

    Key Vault ping only runs when AZURE_KEYVAULT_URL is set AND AZURE_AUTH_DISABLED
    is not true (i.e. production / staging). Exits with code 1 on KV failure so
    the Container App orchestrator can restart and surface the error in logs.
    """

    keyvault_url = os.environ.get("AZURE_KEYVAULT_URL")
    auth_disabled = os.environ.get("AZURE_AUTH_DISABLED", "").lower() in ("1", "true", "yes")

    if keyvault_url and not auth_disabled:
        try:
            from zdrovena.common._keyvault import ping_keyvault

            ping_keyvault(keyvault_url)
            logger.info("Key Vault reachable: %s", keyvault_url)
        except Exception as exc:
            logger.critical(
                "Cannot reach Azure Key Vault at startup: %s — %s. "
                "Secrets (Fakturownia, Zoho, KSeF) will be unavailable. Shutting down.",
                keyvault_url,
                exc,
            )
            sys.exit(1)
    elif auth_disabled:
        logger.info("Key Vault ping skipped — AZURE_AUTH_DISABLED=true (local dev).")
    else:
        logger.info("AZURE_KEYVAULT_URL not set — Key Vault disabled.")

    yield  # app runs


app = FastAPI(
    lifespan=lifespan,
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

# SWA's linked backend routes /api/* to this Container App without stripping
# the /api prefix, so we mount the routers under /api to match what arrives.
app.include_router(close.router, prefix="/api")
app.include_router(files.router, prefix="/api")
app.include_router(invoices.router, prefix="/api")
app.include_router(webhooks.router, prefix="/api")


@app.get("/health", tags=["health"])
def health() -> dict:
    return {"status": "ok", "version": app.version}
