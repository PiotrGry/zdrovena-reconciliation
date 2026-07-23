"""Testy deterministycznej konfiguracji Azure Monitor OpenTelemetry."""

from __future__ import annotations

from unittest.mock import Mock

from fastapi import FastAPI
from opentelemetry.sdk.resources import SERVICE_NAME

from zdrovena.common.telemetry import (
    configure_azure_telemetry,
    force_flush_azure_telemetry,
    instrument_fastapi_app,
)


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


def test_force_flush_flushes_all_configured_signal_providers(monkeypatch):
    trace_provider = Mock()
    metric_provider = Mock()
    log_provider = Mock()
    trace_provider.force_flush.return_value = True
    metric_provider.force_flush.return_value = True
    log_provider.force_flush.return_value = True

    monkeypatch.setattr(
        "opentelemetry.trace.get_tracer_provider",
        lambda: trace_provider,
    )
    monkeypatch.setattr(
        "opentelemetry.metrics.get_meter_provider",
        lambda: metric_provider,
    )
    monkeypatch.setattr(
        "opentelemetry._logs.get_logger_provider",
        lambda: log_provider,
    )

    assert force_flush_azure_telemetry(timeout_millis=1234) is True
    trace_provider.force_flush.assert_called_once_with(timeout_millis=1234)
    metric_provider.force_flush.assert_called_once_with(timeout_millis=1234)
    log_provider.force_flush.assert_called_once_with(timeout_millis=1234)


def test_force_flush_is_non_fatal_when_one_exporter_fails(monkeypatch):
    trace_provider = Mock()
    trace_provider.force_flush.side_effect = RuntimeError("export unavailable")

    monkeypatch.setattr(
        "opentelemetry.trace.get_tracer_provider",
        lambda: trace_provider,
    )
    monkeypatch.setattr("opentelemetry.metrics.get_meter_provider", lambda: object())
    monkeypatch.setattr("opentelemetry._logs.get_logger_provider", lambda: object())

    assert force_flush_azure_telemetry() is False
