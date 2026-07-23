"""Testy bezpiecznego endpointu do walidacji telemetryki na staging."""

from __future__ import annotations

import os

os.environ.setdefault("AZURE_AUTH_DISABLED", "true")

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from zdrovena.api.main import app
from zdrovena.api.routers.monitoring_probe import _require_monitoring_test_support


def test_monitoring_probe_disabled_without_fake_providers(monkeypatch):
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.delenv("PROVIDER_MODE", raising=False)

    client = TestClient(app)
    response = client.get("/api/__test__/monitoring/request")
    client.close()

    assert response.status_code == 404


def test_monitoring_probe_returns_controlled_success(monkeypatch):
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("PROVIDER_MODE", "fake")

    client = TestClient(app)
    response = client.get(
        "/api/__test__/monitoring/request",
        params={"response_status": 200, "delay_ms": 1},
    )
    client.close()

    assert response.status_code == 200
    assert response.json()["status_code"] == 200
    assert response.json()["delay_ms"] == 1
    assert response.headers["X-Correlation-ID"]


def test_monitoring_probe_uses_requested_success_status(monkeypatch):
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("PROVIDER_MODE", "fake")

    client = TestClient(app)
    response = client.get(
        "/api/__test__/monitoring/request",
        params={"response_status": 202},
    )
    client.close()

    assert response.status_code == 202
    assert response.json()["status_code"] == 202


def test_monitoring_probe_returns_controlled_500(monkeypatch):
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("PROVIDER_MODE", "fake")

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get(
        "/api/__test__/monitoring/request",
        params={"response_status": 500},
    )
    client.close()

    assert response.status_code == 500
    assert response.headers["X-Correlation-ID"]
    assert response.json()["detail"] == "Controlled monitoring probe"


def test_monitoring_probe_never_available_in_production(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("PROVIDER_MODE", "fake")

    with pytest.raises(HTTPException) as exc:
        _require_monitoring_test_support()

    assert exc.value.status_code == 404
