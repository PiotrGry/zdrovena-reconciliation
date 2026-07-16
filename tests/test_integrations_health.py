from __future__ import annotations

import os
import time

from fastapi.testclient import TestClient

os.environ.setdefault("AZURE_AUTH_DISABLED", "true")

from zdrovena.api.auth import Principal, get_current_principal
from zdrovena.api.deps import get_shipping, get_storage
from zdrovena.api.main import app


class FakeStorage:
    def list_files(self, prefix: str = "") -> list:
        return []


class FakeShippingStore:
    def list_drafts(self, limit: int = 200) -> list:
        return []

    def list_dlq(self, limit: int = 200) -> list:
        return []


def _health(monkeypatch, *, roles: list[str] | None = None, query: str = "", **env: str):
    names = {
        "APP_ENV",
        "AZURE_AUTH_DISABLED",
        "AZURE_KEYVAULT_URL",
        "AZURE_STORAGE_ACCOUNT_URL",
        "AZURE_STORAGE_CONNECTION_STRING",
        "AZURE_STORAGE_CONTAINER",
        "AZURE_TENANT_ID",
        "AZURE_API_AUDIENCE",
        "SHOPIFY_ALLOWED_DOMAINS",
        "PROVIDER_MODE",
        "FAKTUROWNIA_DISABLED",
        "FAKTUROWNIA_API_TOKEN",
        "FAKTUROWNIA_BASE_URL",
        "ALLEGRO_CLIENT_ID",
        "ALLEGRO_CLIENT_SECRET",
        "ALLEGRO_REFRESH_TOKEN",
        "ALLEGRO_BASE_URL",
        "ALLEGRO_AUTH_URL",
        "ALLEGRO_ENV",
        "INPOST_API_TOKEN",
        "INPOST_ORGANIZATION_ID",
        "INPOST_BASE_URL",
        "APACZKA_APP_ID",
        "APACZKA_APP_SECRET",
    }
    for name in names:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    principal = Principal(
        sub="test-user",
        email="test@example.com",
        roles=roles or ["zdrovena-viewer"],
        name="Test User",
    )
    app.dependency_overrides[get_current_principal] = lambda: principal
    app.dependency_overrides[get_storage] = lambda: FakeStorage()
    app.dependency_overrides[get_shipping] = lambda: FakeShippingStore()
    try:
        with TestClient(app, raise_server_exceptions=True) as client:
            return client.get(f"/api/integrations/health{query}")
    finally:
        app.dependency_overrides.pop(get_current_principal, None)
        app.dependency_overrides.pop(get_storage, None)
        app.dependency_overrides.pop(get_shipping, None)


def _by_key(payload: dict) -> dict[str, dict]:
    return {item["key"]: item for item in payload["integrations"]}


def test_integrations_health_returns_environment_and_statuses(monkeypatch):
    response = _health(
        monkeypatch,
        APP_ENV="staging",
        PROVIDER_MODE="fake",
        AZURE_AUTH_DISABLED="true",
        AZURE_STORAGE_CONNECTION_STRING="UseDevelopmentStorage=true",
        FAKTUROWNIA_API_TOKEN="token",
        FAKTUROWNIA_BASE_URL="http://fake-providers.test/fakturownia",
        ALLEGRO_BASE_URL="http://fake-providers.test/allegro",
        ALLEGRO_AUTH_URL="http://fake-providers.test/allegro/auth/oauth/token",
        INPOST_API_TOKEN="token",
        INPOST_ORGANIZATION_ID="42",
        INPOST_BASE_URL="http://fake-providers.test/inpost",
        APACZKA_BASE_URL="http://fake-providers.test/apaczka/api/v2",
    )

    assert response.status_code == 200
    body = response.json()
    assert body["environment"]["app_env"] == "staging"
    assert body["environment"]["auth_disabled"] is True
    assert body["environment"]["storage_backend"] == "azurite"

    items = _by_key(body)
    assert items["auth"]["status"] == "degraded"
    assert items["storage"]["status"] == "healthy"
    assert items["fakturownia"]["status"] == "healthy"
    assert items["inpost"]["status"] == "healthy"
    assert items["apaczka"]["status"] == "not_configured"
    assert items["inpost"]["checked_at"]
    assert isinstance(items["inpost"]["latency_ms"], int)
    assert items["inpost"]["safe_operation"] == "configuration inspection"
    assert body["operations"][0]["status"] == "not_configured"


def test_integrations_health_flags_production_shopify_allowlist(monkeypatch):
    response = _health(
        monkeypatch,
        APP_ENV="production",
        AZURE_AUTH_DISABLED="false",
        AZURE_TENANT_ID="tenant",
        AZURE_API_AUDIENCE="api-audience",
        ALLEGRO_CLIENT_ID="client-id",
        ALLEGRO_CLIENT_SECRET="client-secret",
        ALLEGRO_REFRESH_TOKEN="refresh-token",
    )

    assert response.status_code == 200
    items = _by_key(response.json())
    assert items["shopify"]["status"] == "unavailable"
    assert items["auth"]["status"] == "healthy"
    assert items["allegro"]["status"] == "healthy"


def test_integrations_health_can_mark_fakturownia_disabled(monkeypatch):
    response = _health(monkeypatch, AZURE_AUTH_DISABLED="true", FAKTUROWNIA_DISABLED="true")

    assert response.status_code == 200
    item = _by_key(response.json())["fakturownia"]
    assert item["status"] == "not_configured"
    assert item["mode"] == "not_configured"


def test_manual_checks_require_admin_role(monkeypatch):
    response = _health(monkeypatch, AZURE_AUTH_DISABLED="true", query="?run_checks=true")

    assert response.status_code == 403
    assert response.json()["detail"] == "Manual integration checks require zdrovena-admin"


def test_admin_can_request_manual_checks(monkeypatch):
    response = _health(
        monkeypatch,
        roles=["zdrovena-admin"],
        AZURE_AUTH_DISABLED="true",
        query="?run_checks=true",
    )

    assert response.status_code == 200


def test_admin_live_checks_use_only_safe_provider_reads(monkeypatch):
    class FakeFakturowniaClient:
        def __init__(self, **kwargs):
            pass

        def list_invoices(self, **kwargs):
            assert kwargs["per_page"] == 1
            return [{}]

        def create_invoice(self, invoice):
            raise AssertionError("health check must not create Fakturownia invoices")

        def update_invoice(self, invoice_id, patch):
            raise AssertionError("health check must not update Fakturownia invoices")

    class FakeInPostClient:
        def __init__(self, *, api_token, organization_id):
            pass

        def get_organization(self):
            return {"id": "org-1"}

        def create_paczkomat_shipment(self, **kwargs):
            raise AssertionError("health check must not create InPost shipments")

        def create_kurier_shipment(self, **kwargs):
            raise AssertionError("health check must not create InPost shipments")

    class FakeApaczkaClient:
        def __init__(self, **kwargs):
            pass

        def _call(self, endpoint, data):
            assert endpoint == "service_structure"
            assert data == {}
            return {"status": 200, "response": {"services": [{}]}}

        def create_shipment(self, **kwargs):
            raise AssertionError("health check must not create Apaczka shipments")

    class FakeAllegroResponse:
        status_code = 200

        def json(self):
            return {"checkoutForms": [{}]}

    def fake_allegro_get(url, *, headers, params, timeout):
        assert url.endswith("/order/checkout-forms")
        assert params == {"limit": 1, "offset": 0}
        return FakeAllegroResponse()

    monkeypatch.setattr("zdrovena.common.fakturownia.FakturowniaClient", FakeFakturowniaClient)
    monkeypatch.setattr("zdrovena.common.inpost.InPostClient", FakeInPostClient)
    monkeypatch.setattr("zdrovena.common.apaczka.ApaczkaClient", FakeApaczkaClient)
    monkeypatch.setattr("zdrovena.api.routers.integrations.requests.get", fake_allegro_get)

    response = _health(
        monkeypatch,
        roles=["zdrovena-admin"],
        AZURE_AUTH_DISABLED="true",
        query="?run_checks=true",
        FAKTUROWNIA_API_TOKEN="token",
        ALLEGRO_CLIENT_ID="client",
        ALLEGRO_CLIENT_SECRET="secret",
        ALLEGRO_REFRESH_TOKEN="refresh",
        ALLEGRO_ACCESS_TOKEN="access",
        ALLEGRO_ACCESS_TOKEN_EXPIRY=str(time.time() + 3600),
        INPOST_API_TOKEN="token",
        INPOST_ORGANIZATION_ID="org-1",
        APACZKA_APP_ID="app",
        APACZKA_APP_SECRET="secret",
    )

    assert response.status_code == 200
    items = _by_key(response.json())
    assert items["fakturownia"]["safe_operation"] == "list invoices page 1"
    assert items["allegro"]["safe_operation"] == "list checkout forms page 1"
    assert items["inpost"]["safe_operation"] == "read organization metadata"
    assert items["apaczka"]["safe_operation"] == "read service catalogue"
    assert items["fakturownia"]["status"] == "healthy"
    assert items["allegro"]["status"] == "healthy"
    assert items["inpost"]["status"] == "healthy"
    assert items["apaczka"]["status"] == "healthy"


def test_allegro_live_check_does_not_rotate_refresh_token(monkeypatch):
    response = _health(
        monkeypatch,
        roles=["zdrovena-admin"],
        AZURE_AUTH_DISABLED="true",
        query="?run_checks=true",
        ALLEGRO_CLIENT_ID="client",
        ALLEGRO_CLIENT_SECRET="secret",
        ALLEGRO_REFRESH_TOKEN="refresh",
    )

    assert response.status_code == 200
    item = _by_key(response.json())["allegro"]
    assert item["status"] == "degraded"
    assert "refresh token rotation is not safe" in item["message"]
