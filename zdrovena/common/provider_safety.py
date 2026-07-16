"""Fail-closed provider routing checks for staging.

Production may use live provider endpoints. Staging must route provider writes to
the fake HTTP provider service and must fail startup if any write-capable client
is still pointed at a known live provider URL.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

from zdrovena.common.appenv import resolve_app_env

FAKE_PROVIDER_MODE = "fake"

_REQUIRED_STAGING_URLS = {
    "ALLEGRO_BASE_URL": ("api.allegro.pl", "api.allegro.pl.allegrosandbox.pl"),
    "ALLEGRO_AUTH_URL": ("allegro.pl", "allegro.pl.allegrosandbox.pl"),
    "INPOST_BASE_URL": ("api-shipx-pl.easypack24.net", "sandbox-api-shipx-pl.easypack24.net"),
    "APACZKA_BASE_URL": ("www.apaczka.pl", "apaczka.pl"),
    "FAKTUROWNIA_BASE_URL": ("fakturownia.pl",),
}


class ProviderSafetyError(RuntimeError):
    """Raised when provider routing is unsafe for the resolved environment."""


def _host_matches(host: str, suffix: str) -> bool:
    host = host.lower().strip(".")
    suffix = suffix.lower().strip(".")
    return host == suffix or host.endswith(f".{suffix}")


def _is_live_provider_url(raw_url: str, live_suffixes: tuple[str, ...]) -> bool:
    parsed = urlparse(raw_url)
    host = parsed.hostname or ""
    return any(_host_matches(host, suffix) for suffix in live_suffixes)


def assert_provider_write_safety() -> None:
    """Validate provider routing for the current environment.

    Staging is intentionally strict: all write-capable provider base URLs must
    be explicitly configured and must not point at known live provider hosts.
    """

    app_env = resolve_app_env()
    if app_env != "staging":
        return

    mode = os.environ.get("PROVIDER_MODE", "").strip().lower()
    if mode != FAKE_PROVIDER_MODE:
        raise ProviderSafetyError(
            "APP_ENV=staging requires PROVIDER_MODE=fake so provider writes cannot hit live APIs."
        )

    missing = [name for name in _REQUIRED_STAGING_URLS if not os.environ.get(name, "").strip()]
    if missing:
        raise ProviderSafetyError(
            "APP_ENV=staging requires fake provider URLs for: " + ", ".join(sorted(missing))
        )

    unsafe: list[str] = []
    for name, live_suffixes in _REQUIRED_STAGING_URLS.items():
        value = os.environ.get(name, "").strip()
        if _is_live_provider_url(value, live_suffixes):
            unsafe.append(f"{name}={value}")
    if unsafe:
        raise ProviderSafetyError(
            "APP_ENV=staging refuses live provider endpoints: " + "; ".join(unsafe)
        )
