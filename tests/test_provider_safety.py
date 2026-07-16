from __future__ import annotations

import pytest

from zdrovena.common.provider_safety import ProviderSafetyError, assert_provider_write_safety

_PROVIDER_URLS = {
    "ALLEGRO_BASE_URL": "http://fake-providers.test/allegro",
    "ALLEGRO_AUTH_URL": "http://fake-providers.test/allegro/auth/oauth/token",
    "INPOST_BASE_URL": "http://fake-providers.test/inpost",
    "APACZKA_BASE_URL": "http://fake-providers.test/apaczka/api/v2",
    "FAKTUROWNIA_BASE_URL": "http://fake-providers.test/fakturownia",
}


def _set_staging_fake_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("PROVIDER_MODE", "fake")
    for name, value in _PROVIDER_URLS.items():
        monkeypatch.setenv(name, value)


def test_staging_requires_fake_provider_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.delenv("PROVIDER_MODE", raising=False)

    with pytest.raises(ProviderSafetyError, match="PROVIDER_MODE=fake"):
        assert_provider_write_safety()


def test_staging_requires_all_provider_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_staging_fake_env(monkeypatch)
    monkeypatch.delenv("INPOST_BASE_URL")

    with pytest.raises(ProviderSafetyError, match="requires fake provider URLs"):
        assert_provider_write_safety()


def test_staging_rejects_live_provider_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_staging_fake_env(monkeypatch)
    monkeypatch.setenv("INPOST_BASE_URL", "https://api-shipx-pl.easypack24.net")

    with pytest.raises(ProviderSafetyError, match="refuses live provider endpoints"):
        assert_provider_write_safety()


def test_staging_accepts_fake_provider_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_staging_fake_env(monkeypatch)

    assert_provider_write_safety()


def test_production_is_not_forced_to_fake_provider_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.delenv("PROVIDER_MODE", raising=False)
    monkeypatch.setenv("INPOST_BASE_URL", "https://api-shipx-pl.easypack24.net")

    assert_provider_write_safety()
