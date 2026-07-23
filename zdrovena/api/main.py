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

from zdrovena.api.errors import install_exception_handlers
from zdrovena.api.observability import CorrelationIdFilter, correlation_id_middleware
from zdrovena.api.routers import (
    close,
    damage,
    files,
    integrations,
    invoices,
    monitoring_probe,
    webhooks,
)
from zdrovena.common.appenv import UNKNOWN_ENV, is_production_env, resolve_app_env
from zdrovena.common.provider_safety import ProviderSafetyError, assert_provider_write_safety
from zdrovena.common.telemetry import configure_azure_telemetry, instrument_fastapi_app

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s [%(correlation_id)s]: %(message)s",
    force=True,
)
# Bez tego filtra %(correlation_id)s w formacie rzuciłby KeyError dla rekordów
# spoza żądania (start aplikacji, logi bibliotek). Filtr na handlerze root
# gwarantuje, że KAŻDY rekord ma atrybut correlation_id.
for _root_handler in logging.getLogger().handlers:
    _root_handler.addFilter(CorrelationIdFilter())

# Azure SDK's http_logging_policy emits every request/response header at INFO,
# which floods the console during dev (~30 lines per Azurite/Table call). Pin
# the noisy Azure SDK loggers to a configurable level (default WARNING). Set
# LOG_LEVEL_AZURE=DEBUG when you actually need to inspect HTTP traffic.
_azure_log_level = os.environ.get("LOG_LEVEL_AZURE", "WARNING").upper()
for _name in (
    "azure.core.pipeline.policies.http_logging_policy",
    "azure.identity",
    "azure.storage",
    "azure.data.tables",
    "azure.monitor.opentelemetry",
):
    logging.getLogger(_name).setLevel(_azure_log_level)

logger = logging.getLogger("zdrovena.api.main")

_telemetry_environment = resolve_app_env()
_default_service_name = {
    "production": "zdrovena-api-prod",
    "staging": "zdrovena-api-staging",
    "development": "zdrovena-api-development",
}.get(_telemetry_environment or "", "zdrovena-api")
_azure_telemetry_enabled = configure_azure_telemetry(default_service_name=_default_service_name)


def _is_production_env() -> bool:
    """True gdy kanoniczny ``APP_ENV`` wskazuje deploy produkcyjny.

    Delegates to :func:`zdrovena.common.appenv.is_production_env` — jedno
    kanoniczne źródło rozstrzygania środowiska dla całej aplikacji (R4-B).
    """
    return is_production_env()


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[type-arg]
    """Startup: strażnik AZURE_AUTH_DISABLED + weryfikacja dostępności Key Vault.

    Key Vault ping only runs when AZURE_KEYVAULT_URL is set AND AZURE_AUTH_DISABLED
    is not true (i.e. production / staging). Exits with code 1 on KV failure so
    the Container App orchestrator can restart and surface the error in logs.
    """

    keyvault_url = os.environ.get("AZURE_KEYVAULT_URL")
    auth_disabled = os.environ.get("AZURE_AUTH_DISABLED", "").lower() in ("1", "true", "yes")
    app_env = resolve_app_env()
    # A Key Vault URL is only ever configured for a real Azure deployment; local
    # dev has none. It is our "am I deployed?" signal for the fail-closed rules.
    deployed = bool(keyvault_url)

    # Strażnik 1: jawnie ustawione, ale nierozpoznane APP_ENV jest niejednoznaczne
    # — nie potrafimy stwierdzić, czy to produkcja. Fail-closed.
    if app_env == UNKNOWN_ENV:
        logger.critical(
            "APP_ENV ustawione na nierozpoznaną wartość — środowisko jest niejednoznaczne. "
            "Odmawiam startu. Ustaw APP_ENV na jedno z: development / staging / production."
        )
        sys.exit(1)

    # Strażnik 2: uruchomienie w produkcji z wyłączoną autoryzacją to krytyczna
    # dziura bezpieczeństwa (każdy JWT przechodzi). Odmawiamy startu, żeby
    # orchestrator Container App zgłosił błąd zamiast wystawić otwarte API.
    if auth_disabled and _is_production_env():
        logger.critical(
            "AZURE_AUTH_DISABLED=true w środowisku produkcyjnym — API byłoby otwarte "
            "bez autoryzacji. Odmawiam startu. Usuń AZURE_AUTH_DISABLED z konfiguracji "
            "produkcyjnej."
        )
        sys.exit(1)

    # Strażnik 3: wyłączona autoryzacja w realnym deploymencie (Key Vault ustawiony)
    # bez jawnego APP_ENV=development jest niejednoznaczne — traktujemy jak potencjalną
    # produkcję i odmawiamy startu. Lokalny dev (bez Key Vault) nie jest tym objęty.
    if auth_disabled and deployed and app_env != "development":
        logger.critical(
            "AZURE_AUTH_DISABLED=true w deploymencie (AZURE_KEYVAULT_URL ustawione), a APP_ENV "
            "!= development — niejednoznaczne, potencjalnie produkcyjne, otwarte API. Odmawiam "
            "startu. Ustaw APP_ENV=development, jeśli to celowe środowisko deweloperskie."
        )
        sys.exit(1)

    try:
        assert_provider_write_safety()
    except ProviderSafetyError as exc:
        logger.critical("Unsafe provider routing: %s", exc)
        sys.exit(1)

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
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Correlation-ID"],
    expose_headers=["X-Correlation-ID"],
)

# Correlation ID: akceptuj/generuj X-Correlation-ID, echo w odpowiedzi, wstrzyknij
# do logów i koperty błędu. Rejestrowane po CORS, aby nagłówek był widoczny w SPA.
app.middleware("http")(correlation_id_middleware)

# SWA's linked backend routes /api/* to this Container App without stripping
# the /api prefix, so we mount the routers under /api to match what arrives.
app.include_router(close.router, prefix="/api")
app.include_router(files.router, prefix="/api")
app.include_router(integrations.router, prefix="/api")
app.include_router(invoices.router, prefix="/api")
app.include_router(webhooks.router, prefix="/api")
app.include_router(damage.router, prefix="/api")
app.include_router(monitoring_probe.router, prefix="/api")

# Jednolita koperta błędu: mapuje wyjątki przesyłkowe na polskie komunikaty
# i przechwytuje nieobsłużone wyjątki zamiast wyciekać surowy str(exc).
install_exception_handlers(app)

# Azure Monitor distro podmienia ``fastapi.FastAPI`` w ramach auto-instrumentacji.
# Ta aplikacja importuje klasę przed konfiguracją telemetryki, dlatego jawnie
# instrumentujemy gotową instancję. Bez tego traces/dependencies działają, ale
# request spans nie trafiają do AppRequests.
if _azure_telemetry_enabled:
    instrument_fastapi_app(app)


@app.get("/health", tags=["health"])
def health() -> dict:
    return {"status": "ok", "version": app.version}
