"""zdrovena.common.appenv — canonical application-environment resolution.

R4-B: the codebase previously scattered environment detection across
``APP_ENV``/``DEPLOY_ENV``/``AZURE_ENV``/``ENV`` in both ``api.main`` and
``api.routers.webhooks``. That made "is this production?" answerable in two
places that could drift. This module is the single source of truth.

``APP_ENV`` is the canonical variable. The three legacy variables are still
honoured as a deprecation fallback so existing deployments keep working, but
new configuration should set only ``APP_ENV``.

Recognised values (case-insensitive, whitespace-trimmed):
  * production : ``production`` | ``prod`` | ``live``
  * staging    : ``staging`` | ``stage``
  * development: ``development`` | ``dev`` | ``local`` | ``sandbox``

A value that is set but matches none of the above resolves to
:data:`UNKNOWN_ENV` — an *ambiguous* environment that startup treats as unsafe.
"""

from __future__ import annotations

import os

UNKNOWN_ENV = "__unknown__"

_CANONICAL_VAR = "APP_ENV"
_LEGACY_VARS = ("DEPLOY_ENV", "AZURE_ENV", "ENV")

_ALIASES: dict[str, str] = {
    "production": "production",
    "prod": "production",
    "live": "production",
    "staging": "staging",
    "stage": "staging",
    "development": "development",
    "dev": "development",
    "local": "development",
    "sandbox": "development",
}


def resolve_app_env() -> str | None:
    """Return the canonical environment name, ``UNKNOWN_ENV``, or ``None``.

    ``None`` means no environment variable is set at all. ``UNKNOWN_ENV`` means
    one was set to a value we do not recognise (ambiguous — callers should treat
    it as unsafe). Otherwise one of ``production`` / ``staging`` / ``development``.
    """
    for var in (_CANONICAL_VAR, *_LEGACY_VARS):
        raw = os.environ.get(var, "").strip().lower()
        if raw:
            return _ALIASES.get(raw, UNKNOWN_ENV)
    return None


def is_production_env() -> bool:
    """True only when the resolved environment is production."""
    return resolve_app_env() == "production"
