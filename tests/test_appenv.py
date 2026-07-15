"""Tests for zdrovena.common.appenv — canonical APP_ENV resolution (R4-B)."""

from __future__ import annotations

import pytest

from zdrovena.common.appenv import UNKNOWN_ENV, is_production_env, resolve_app_env

_ENV_VARS = ("APP_ENV", "DEPLOY_ENV", "AZURE_ENV", "ENV")


def _clear(monkeypatch) -> None:
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)


class TestResolveAppEnv:
    def test_unset_returns_none(self, monkeypatch):
        _clear(monkeypatch)
        assert resolve_app_env() is None

    @pytest.mark.parametrize("value", ["production", "prod", "live", "PRODUCTION", " Prod "])
    def test_production_aliases(self, monkeypatch, value):
        _clear(monkeypatch)
        monkeypatch.setenv("APP_ENV", value)
        assert resolve_app_env() == "production"

    @pytest.mark.parametrize("value", ["staging", "stage", "STAGING"])
    def test_staging_aliases(self, monkeypatch, value):
        _clear(monkeypatch)
        monkeypatch.setenv("APP_ENV", value)
        assert resolve_app_env() == "staging"

    @pytest.mark.parametrize("value", ["development", "dev", "local", "sandbox"])
    def test_development_aliases(self, monkeypatch, value):
        _clear(monkeypatch)
        monkeypatch.setenv("APP_ENV", value)
        assert resolve_app_env() == "development"

    def test_unrecognized_value_is_unknown(self, monkeypatch):
        _clear(monkeypatch)
        monkeypatch.setenv("APP_ENV", "prd-typo")
        assert resolve_app_env() == UNKNOWN_ENV

    def test_canonical_var_takes_precedence(self, monkeypatch):
        _clear(monkeypatch)
        monkeypatch.setenv("APP_ENV", "development")
        monkeypatch.setenv("DEPLOY_ENV", "production")
        assert resolve_app_env() == "development"

    def test_legacy_var_fallback(self, monkeypatch):
        _clear(monkeypatch)
        monkeypatch.setenv("DEPLOY_ENV", "production")
        assert resolve_app_env() == "production"


class TestIsProductionEnv:
    def test_true_for_production(self, monkeypatch):
        _clear(monkeypatch)
        monkeypatch.setenv("APP_ENV", "production")
        assert is_production_env() is True

    @pytest.mark.parametrize("value", ["staging", "development", "prd-typo", ""])
    def test_false_for_non_production(self, monkeypatch, value):
        _clear(monkeypatch)
        if value:
            monkeypatch.setenv("APP_ENV", value)
        assert is_production_env() is False
