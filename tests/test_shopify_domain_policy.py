"""Tests for the Shopify domain-allow-list fail-closed policy (P1-6).

_is_shopify_domain_allowed used to log a warning and accept any caller when
SHOPIFY_ALLOWED_DOMAINS was unset. That was safe for local dev but silently
disabled the whitelist in production. The new policy is fail-closed in
production and only permissive in non-production environments.
"""

from __future__ import annotations

import pytest

from zdrovena.api.routers.webhooks import (
    _is_production_env,
    _is_shopify_domain_allowed,
)


class TestIsProductionEnv:
    def test_unset_is_not_production(self, monkeypatch):
        for var in ("APP_ENV", "DEPLOY_ENV", "AZURE_ENV", "ENV"):
            monkeypatch.delenv(var, raising=False)
        assert _is_production_env() is False

    @pytest.mark.parametrize("value", ["production", "prod", "live", "PROD", "Production"])
    def test_recognises_production_values(self, monkeypatch, value):
        for var in ("APP_ENV", "DEPLOY_ENV", "AZURE_ENV", "ENV"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("APP_ENV", value)
        assert _is_production_env() is True

    @pytest.mark.parametrize("value", ["dev", "development", "staging", "sandbox", "test", ""])
    def test_non_production_values(self, monkeypatch, value):
        for var in ("APP_ENV", "DEPLOY_ENV", "AZURE_ENV", "ENV"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("APP_ENV", value)
        assert _is_production_env() is False

    def test_any_of_the_env_vars_triggers_production(self, monkeypatch):
        for var in ("APP_ENV", "DEPLOY_ENV", "AZURE_ENV", "ENV"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("DEPLOY_ENV", "production")
        assert _is_production_env() is True


class TestDomainPolicyFailClosed:
    def _clear(self, monkeypatch):
        for var in ("APP_ENV", "DEPLOY_ENV", "AZURE_ENV", "ENV", "SHOPIFY_ALLOWED_DOMAINS"):
            monkeypatch.delenv(var, raising=False)

    def test_whitelist_allows_matching_domain(self, monkeypatch):
        self._clear(monkeypatch)
        monkeypatch.setenv("SHOPIFY_ALLOWED_DOMAINS", "zdrovena.myshopify.com")
        assert _is_shopify_domain_allowed("zdrovena.myshopify.com") is True

    def test_whitelist_rejects_non_matching_domain(self, monkeypatch):
        self._clear(monkeypatch)
        monkeypatch.setenv("SHOPIFY_ALLOWED_DOMAINS", "zdrovena.myshopify.com")
        assert _is_shopify_domain_allowed("evil.myshopify.com") is False

    def test_whitelist_is_case_insensitive(self, monkeypatch):
        self._clear(monkeypatch)
        monkeypatch.setenv("SHOPIFY_ALLOWED_DOMAINS", "Zdrovena.MyShopify.com")
        assert _is_shopify_domain_allowed("zdrovena.myshopify.com") is True

    def test_multiple_whitelisted_domains(self, monkeypatch):
        self._clear(monkeypatch)
        monkeypatch.setenv("SHOPIFY_ALLOWED_DOMAINS", "a.myshopify.com, b.myshopify.com")
        assert _is_shopify_domain_allowed("a.myshopify.com") is True
        assert _is_shopify_domain_allowed("b.myshopify.com") is True
        assert _is_shopify_domain_allowed("c.myshopify.com") is False

    def test_unset_in_production_rejects(self, monkeypatch):
        """P1-6: unset whitelist in production must fail closed."""
        self._clear(monkeypatch)
        monkeypatch.setenv("APP_ENV", "production")
        assert _is_shopify_domain_allowed("zdrovena.myshopify.com") is False

    def test_unset_in_production_rejects_via_deploy_env(self, monkeypatch):
        self._clear(monkeypatch)
        monkeypatch.setenv("DEPLOY_ENV", "prod")
        assert _is_shopify_domain_allowed("any.myshopify.com") is False

    def test_unset_in_dev_still_permissive(self, monkeypatch):
        """Local dev / sandbox / staging keep the permissive behaviour with a warning."""
        self._clear(monkeypatch)
        monkeypatch.setenv("APP_ENV", "development")
        assert _is_shopify_domain_allowed("zdrovena.myshopify.com") is True

    def test_unset_with_no_env_marker_stays_permissive(self, monkeypatch):
        """No env markers = dev workflow default."""
        self._clear(monkeypatch)
        assert _is_shopify_domain_allowed("zdrovena.myshopify.com") is True

    def test_missing_shop_domain_header_in_production_fails_closed(self, monkeypatch):
        self._clear(monkeypatch)
        monkeypatch.setenv("APP_ENV", "production")
        assert _is_shopify_domain_allowed("") is False
