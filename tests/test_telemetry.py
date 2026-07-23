"""Testy deterministycznej konfiguracji Azure Monitor OpenTelemetry."""

from __future__ import annotations

from unittest.mock import Mock

from fastapi import FastAPI
from opentelemetry.sdk.resources import SERVICE_NAME

from zdrovena.common.telemetry import configure_azure_telemetry, instrument_fastapi_app


def test_telemetry_disabled_without_connection_string(monkeypatch):
    monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)

    assert configure_azure_telemetry(default_service_name="zdrovena-api") is False


def test_telemetry_sets_explicit_service_name_and_disables_fastapi_auto(monkeypatch):
    monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "InstrumentationKey=test")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "zdrovena-api-staging")

    configure = Mock()
    monkeypatch.setattr("azure.monitor.opentelemetry.configure_azure_monitor", configure)

    assert configure_azure_telemetry(default_service_name="fallback") is True

    kwargs = configure.call_args.kwargs
    assert kwargs["resource"].attributes[SERVICE_NAME] == "zdrovena-api-staging"
    assert kwargs["instrumentation_options"] == {"fastapi": {"enabled": False}}


def test_telemetry_uses_default_service_name(monkeypatch):
    monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "InstrumentationKey=test")
    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)

    configure = Mock()
    monkeypatch.setattr("azure.monitor.opentelemetry.configure_azure_monitor", configure)

    assert configure_azure_telemetry(default_service_name="zdrovena-allegro-poller") is True
    assert (
        configure.call_args.kwargs["resource"].attributes[SERVICE_NAME] == "zdrovena-allegro-poller"
    )


def test_fastapi_app_is_instrumented_explicitly(monkeypatch):
    app = FastAPI()
    instrument = Mock()
    monkeypatch.setattr(
        "opentelemetry.instrumentation.fastapi.FastAPIInstrumentor.instrument_app",
        instrument,
    )

    instrument_fastapi_app(app)

    instrument.assert_called_once_with(app)
