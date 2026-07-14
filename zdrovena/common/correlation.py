"""zdrovena.common.correlation — prymitywy correlation ID (bez zależności od web).

Sam identyfikator korelacji i filtr logów są niezależne od frameworka, więc
żyją w warstwie liściowej ``common`` (patrz reguły granic modułów w
``tests/fitness/test_module_boundaries.py``). Warstwa web (FastAPI middleware)
budowana jest na tym w ``zdrovena.api.observability``.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import re
import uuid
from collections.abc import Iterator

correlation_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default="-"
)

# R4-B: an incoming X-Correlation-ID (or X-Shopify-Webhook-Id) is attacker-
# controlled. Cap the length and restrict the charset so it is safe to embed in
# logs, response headers and the error envelope (no CRLF injection, no log
# flooding). Anything outside this shape is replaced with a fresh generated ID.
MAX_CORRELATION_ID_LEN = 128
_VALID_CORRELATION_ID = re.compile(r"\A[A-Za-z0-9._-]{1,128}\Z")


def get_correlation_id() -> str:
    """Zwróć correlation ID bieżącego kontekstu (``"-"`` poza żądaniem)."""
    return correlation_id_var.get()


def set_correlation_id(value: str) -> contextvars.Token[str]:
    """Ustaw correlation ID (używane przez zadania tła). Puste → ``"-"``."""
    return correlation_id_var.set(value or "-")


def new_correlation_id() -> str:
    return uuid.uuid4().hex[:12]


def sanitize_correlation_id(raw: str | None) -> str:
    """Return ``raw`` when it is a valid correlation ID, else a fresh safe ID.

    Valid = 1–128 chars of ``[A-Za-z0-9._-]``. Empty, oversized or otherwise
    malformed input (control characters, spaces, injection attempts) is
    replaced deterministically with a newly generated ID.
    """
    candidate = (raw or "").strip()
    if candidate and _VALID_CORRELATION_ID.match(candidate):
        return candidate
    return new_correlation_id()


@contextlib.contextmanager
def correlation_scope(value: str | None) -> Iterator[str]:
    """Bind a correlation ID for the duration of a block, always resetting.

    Used by background tasks (which run after the request context is torn down)
    so the ContextVar is set from a token and reset in ``finally`` — preventing
    an ID from one task leaking into the next task on the same worker.
    """
    cid = sanitize_correlation_id(value)
    token = correlation_id_var.set(cid)
    try:
        yield cid
    finally:
        correlation_id_var.reset(token)


class CorrelationIdFilter(logging.Filter):
    """Dokleja ``correlation_id`` do każdego rekordu logu przechodzącego handler."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = correlation_id_var.get()
        return True
