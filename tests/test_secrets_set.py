"""Tests for ``zdrovena.common.secrets.set_secret`` (P0-2 dependency).

Ensures a rotated OAuth token is persisted through the correct backend:
Azure Key Vault when AZURE_KEYVAULT_URL is set, keyring otherwise, and a
warning-with-env-var-fallback when neither is available.
"""

from __future__ import annotations

import logging
import os
from unittest.mock import patch

import pytest

from zdrovena.common import secrets as secrets_mod


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Ensure tests don't leak AZURE_KEYVAULT_URL or the target env var."""
    monkeypatch.delenv("AZURE_KEYVAULT_URL", raising=False)
    monkeypatch.delenv("ALLEGRO_REFRESH_TOKEN", raising=False)
    yield


class TestSetSecretKeyVault:
    def test_writes_to_keyvault_when_url_set(self, monkeypatch):
        monkeypatch.setenv("AZURE_KEYVAULT_URL", "https://vault.example/")
        with patch("zdrovena.common._keyvault.set_keyvault_secret", return_value=True) as kv:
            with patch.object(secrets_mod, "_KEYRING_AVAILABLE", False):
                assert secrets_mod.set_secret("allegro-refresh-token", "rt-1") is True
        kv.assert_called_once_with("https://vault.example/", "allegro-refresh-token", "rt-1")

    def test_returns_false_when_keyvault_fails_and_no_keyring(self, monkeypatch, caplog):
        monkeypatch.setenv("AZURE_KEYVAULT_URL", "https://vault.example/")
        with patch("zdrovena.common._keyvault.set_keyvault_secret", return_value=False):
            with patch.object(secrets_mod, "_KEYRING_AVAILABLE", False):
                with caplog.at_level(logging.WARNING, logger="zdrovena.common.secrets"):
                    result = secrets_mod.set_secret("allegro-refresh-token", "rt-1")
        assert result is False
        # env-var fallback is set so process keeps working
        assert os.environ.get("ALLEGRO_REFRESH_TOKEN") == "rt-1"
        assert any("could not be persisted" in r.message for r in caplog.records)


class TestSetSecretKeyring:
    def test_writes_to_keyring_when_available(self, monkeypatch):
        # No Key Vault
        fake_keyring = type("FK", (), {})()
        called = {}

        def _set(service, account, value):
            called["args"] = (service, account, value)

        fake_keyring.set_password = _set
        with patch.object(secrets_mod, "keyring", fake_keyring, create=True):
            with patch.object(secrets_mod, "_KEYRING_AVAILABLE", True):
                assert secrets_mod.set_secret("srv", "v") is True
        assert called["args"][0] == "srv"
        assert called["args"][2] == "v"


class TestSetSecretFallback:
    def test_env_var_fallback_when_nothing_available(self, caplog):
        with patch.object(secrets_mod, "_KEYRING_AVAILABLE", False):
            with caplog.at_level(logging.WARNING, logger="zdrovena.common.secrets"):
                result = secrets_mod.set_secret("allegro-refresh-token", "rt-fallback")
        assert result is False
        assert os.environ.get("ALLEGRO_REFRESH_TOKEN") == "rt-fallback"
