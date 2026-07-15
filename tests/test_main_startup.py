"""Testy startowego strażnika AZURE_AUTH_DISABLED + CORS (PATCH/OPTIONS) w main."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from zdrovena.api.main import _is_production_env, app, lifespan

_ENV_VARS = ("APP_ENV", "DEPLOY_ENV", "AZURE_ENV", "ENV")


def _clear_env(monkeypatch) -> None:
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def _run_lifespan() -> None:
    async def _go() -> None:
        async with lifespan(app):
            pass

    asyncio.run(_go())


class TestProductionEnvDetection:
    @pytest.mark.parametrize("var", _ENV_VARS)
    @pytest.mark.parametrize("value", ["production", "prod", "live", "PRODUCTION", " Prod "])
    def test_production_values_detected(self, monkeypatch, var, value):
        _clear_env(monkeypatch)
        monkeypatch.setenv(var, value)
        assert _is_production_env() is True

    def test_unset_is_not_production(self, monkeypatch):
        _clear_env(monkeypatch)
        assert _is_production_env() is False

    @pytest.mark.parametrize("value", ["staging", "sandbox", "development", "dev", ""])
    def test_non_production_values(self, monkeypatch, value):
        _clear_env(monkeypatch)
        monkeypatch.setenv("APP_ENV", value)
        assert _is_production_env() is False


class TestAuthDisabledGuard:
    def test_refuses_boot_in_production_with_auth_disabled(self, monkeypatch):
        _clear_env(monkeypatch)
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("AZURE_AUTH_DISABLED", "true")
        monkeypatch.delenv("AZURE_KEYVAULT_URL", raising=False)
        with pytest.raises(SystemExit) as exc:
            _run_lifespan()
        assert exc.value.code == 1

    def test_boots_in_dev_with_auth_disabled(self, monkeypatch):
        _clear_env(monkeypatch)
        monkeypatch.setenv("AZURE_AUTH_DISABLED", "true")
        monkeypatch.delenv("AZURE_KEYVAULT_URL", raising=False)
        _run_lifespan()  # brak SystemExit

    def test_boots_in_production_with_auth_enabled(self, monkeypatch):
        _clear_env(monkeypatch)
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("AZURE_AUTH_DISABLED", "false")
        monkeypatch.delenv("AZURE_KEYVAULT_URL", raising=False)
        _run_lifespan()  # brak SystemExit (KV nie skonfigurowany)


class TestAmbiguousEnvGuard:
    """R4-B: fail-closed on ambiguous environment / auth combinations."""

    def test_unknown_app_env_refuses_boot(self, monkeypatch):
        _clear_env(monkeypatch)
        monkeypatch.setenv("APP_ENV", "prd-typo")  # nierozpoznane → niejednoznaczne
        monkeypatch.setenv("AZURE_AUTH_DISABLED", "false")
        monkeypatch.delenv("AZURE_KEYVAULT_URL", raising=False)
        with pytest.raises(SystemExit) as exc:
            _run_lifespan()
        assert exc.value.code == 1

    def test_deployed_with_auth_disabled_and_no_env_refuses_boot(self, monkeypatch):
        # Key Vault set (deployment) + auth off + APP_ENV unset → ambiguous prod.
        _clear_env(monkeypatch)
        monkeypatch.setenv("AZURE_AUTH_DISABLED", "true")
        monkeypatch.setenv("AZURE_KEYVAULT_URL", "https://kv.example.net/")
        with pytest.raises(SystemExit) as exc:
            _run_lifespan()
        assert exc.value.code == 1

    def test_deployed_with_auth_disabled_and_staging_refuses_boot(self, monkeypatch):
        _clear_env(monkeypatch)
        monkeypatch.setenv("APP_ENV", "staging")
        monkeypatch.setenv("AZURE_AUTH_DISABLED", "true")
        monkeypatch.setenv("AZURE_KEYVAULT_URL", "https://kv.example.net/")
        with pytest.raises(SystemExit) as exc:
            _run_lifespan()
        assert exc.value.code == 1

    def test_deployed_with_auth_disabled_and_explicit_dev_boots(self, monkeypatch):
        # Explicit development env is the one safe way to disable auth in a
        # Key-Vault-backed context — KV ping is skipped when auth is disabled.
        _clear_env(monkeypatch)
        monkeypatch.setenv("APP_ENV", "development")
        monkeypatch.setenv("AZURE_AUTH_DISABLED", "true")
        monkeypatch.setenv("AZURE_KEYVAULT_URL", "https://kv.example.net/")
        _run_lifespan()  # brak SystemExit

    def test_local_dev_no_keyvault_still_boots_without_app_env(self, monkeypatch):
        # Backward-compat: local dev (no Key Vault) with auth off and no APP_ENV
        # must still start — the deployment guard only fires when KV is set.
        _clear_env(monkeypatch)
        monkeypatch.setenv("AZURE_AUTH_DISABLED", "true")
        monkeypatch.delenv("AZURE_KEYVAULT_URL", raising=False)
        _run_lifespan()  # brak SystemExit


class TestCorsMethods:
    @pytest.mark.parametrize("method", ["PATCH", "OPTIONS", "GET", "POST", "PUT", "DELETE"])
    def test_cors_preflight_allows_method(self, method):
        client = TestClient(app)
        resp = client.options(
            "/api/whatever",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": method,
            },
        )
        allow = resp.headers.get("access-control-allow-methods", "")
        assert method in allow
