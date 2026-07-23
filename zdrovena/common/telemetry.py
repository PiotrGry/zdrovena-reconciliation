"""Deterministyczna konfiguracja Azure Monitor OpenTelemetry.

Azure Monitor distro automatycznie instrumentuje FastAPI przez podmianę
``fastapi.FastAPI``. To nie działa, gdy aplikacja zaimportowała klasę przed
wywołaniem ``configure_azure_monitor``. Dlatego wyłączamy automatyczną
instrumentację FastAPI i jawnie instrumentujemy gotową instancję aplikacji.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger("zdrovena.common.telemetry")


def configure_azure_telemetry(*, default_service_name: str) -> bool:
    """Skonfiguruj eksport Azure Monitor i zwróć, czy został włączony.

    ``OTEL_SERVICE_NAME`` ma pierwszeństwo przed wartością domyślną. Jawny
    resource zapobiega raportowaniu ``AppRoleName=unknown_service``.
    """

    if not os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING"):
        return False

    service_name = os.environ.get("OTEL_SERVICE_NAME", "").strip() or default_service_name

    try:
        from azure.monitor.opentelemetry import configure_azure_monitor
        from opentelemetry.sdk.resources import SERVICE_NAME, Resource

        configure_azure_monitor(
            resource=Resource.create({SERVICE_NAME: service_name}),
            # FastAPI jest instrumentowane jawnie po utworzeniu aplikacji.
            # Pozostałe integracje distro (requests, Azure SDK, logging) zostają.
            instrumentation_options={"fastapi": {"enabled": False}},
        )
    except Exception as exc:
        logger.warning("Azure Monitor configuration failed (non-fatal): %s", exc)
        return False

    logger.info("Azure Monitor OpenTelemetry configured (service.name=%s).", service_name)
    return True


def force_flush_azure_telemetry(*, timeout_millis: int = 5000) -> bool:
    """Opróżnij bufory OpenTelemetry przed zakończeniem krótkiego procesu.

    Azure Container App Jobs kończą się szybciej niż okresowe procesory
    eksportera. Jawny flush zapobiega utracie ostatnich logów, spanów i metryk.
    Brak skonfigurowanego SDK jest poprawnym no-opem; błąd eksportera nie może
    zmienić wyniku zakończonego cyklu biznesowego.
    """

    from opentelemetry import metrics, trace
    from opentelemetry._logs import get_logger_provider

    providers = (
        ("traces", trace.get_tracer_provider()),
        ("metrics", metrics.get_meter_provider()),
        ("logs", get_logger_provider()),
    )
    attempted = False
    success = True

    for signal_name, provider in providers:
        force_flush = getattr(provider, "force_flush", None)
        if not callable(force_flush):
            continue
        attempted = True
        try:
            if force_flush(timeout_millis=timeout_millis) is False:
                logger.warning("Azure Monitor %s flush timed out.", signal_name)
                success = False
        except Exception as exc:
            logger.warning("Azure Monitor %s flush failed (non-fatal): %s", signal_name, exc)
            success = False

    return attempted and success


def instrument_fastapi_app(app: Any) -> None:
    """Jawnie opakuj instancję FastAPI instrumentacją requestów."""

    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(app)
