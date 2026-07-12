"""zdrovena.api.observability — correlation ID (contextvar) + middleware + log filter.

Wiąże pojedyncze żądanie HTTP z jego logami (również w ``BackgroundTasks``) przez
jeden identyfikator korelacji. Middleware akceptuje przychodzący
``X-Correlation-ID`` (lub ``X-Shopify-Webhook-Id`` z webhooków Shopify), a gdy go
brak — generuje nowy. ID jest:

* echo'wane w nagłowku odpowiedzi ``X-Correlation-ID``,
* wstrzykiwane do każdego rekordu logu (``CorrelationIdFilter``),
* wstawiane do koperty błędu (``zdrovena.api.errors``).

``BackgroundTasks`` w Starlette wykonują się po zresetowaniu kontekstu żądania,
więc correlation ID trzeba przekazać jawnie do zadania tła i ustawić go tam
ponownie przez :func:`set_correlation_id`.
"""

from __future__ import annotations

import contextvars
import logging
import uuid
from collections.abc import Awaitable, Callable

from fastapi import Request, Response

CORRELATION_HEADER = "X-Correlation-ID"
_WEBHOOK_ID_HEADER = "X-Shopify-Webhook-Id"

correlation_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default="-"
)


def get_correlation_id() -> str:
    """Zwróć correlation ID bieżącego kontekstu (``"-"`` poza żądaniem)."""
    return correlation_id_var.get()


def set_correlation_id(value: str) -> contextvars.Token[str]:
    """Ustaw correlation ID (używane przez zadania tła). Puste → ``"-"``."""
    return correlation_id_var.set(value or "-")


def new_correlation_id() -> str:
    return uuid.uuid4().hex[:12]


class CorrelationIdFilter(logging.Filter):
    """Dokleja ``correlation_id`` do każdego rekordu logu przechodzącego handler."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = correlation_id_var.get()
        return True


async def correlation_id_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    incoming = (
        request.headers.get(CORRELATION_HEADER) or request.headers.get(_WEBHOOK_ID_HEADER) or ""
    ).strip()
    cid = incoming or new_correlation_id()
    token = correlation_id_var.set(cid)
    try:
        response = await call_next(request)
        response.headers[CORRELATION_HEADER] = cid
        return response
    finally:
        correlation_id_var.reset(token)
