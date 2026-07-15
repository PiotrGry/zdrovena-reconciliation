"""zdrovena.api.observability — warstwa web correlation ID (middleware FastAPI).

Prymitywy (contextvar, helpery, filtr logów) żyją w ``zdrovena.common.correlation``
(warstwa liściowa, bez zależności od frameworka). Tutaj dokładamy wyłącznie
middleware HTTP i re-eksportujemy prymitywy dla wygody importu w warstwie api.

Correlation ID wiąże żądanie HTTP z jego logami (również w ``BackgroundTasks``).
Middleware akceptuje przychodzący ``X-Correlation-ID`` (lub ``X-Shopify-Webhook-Id``
z webhooków Shopify), a gdy go brak — generuje nowy. ID jest echo'wane w nagłowku
odpowiedzi, wstrzykiwane do logów i do koperty błędu (``zdrovena.api.errors``).

``BackgroundTasks`` w Starlette wykonują się po zresetowaniu kontekstu żądania,
więc correlation ID trzeba przekazać jawnie do zadania tła i ustawić go tam
ponownie przez :func:`set_correlation_id`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Request, Response

from zdrovena.common.correlation import (
    CorrelationIdFilter,
    correlation_id_var,
    correlation_scope,
    get_correlation_id,
    new_correlation_id,
    sanitize_correlation_id,
    set_correlation_id,
)

__all__ = [
    "CORRELATION_HEADER",
    "CorrelationIdFilter",
    "correlation_id_middleware",
    "correlation_id_var",
    "correlation_scope",
    "get_correlation_id",
    "new_correlation_id",
    "sanitize_correlation_id",
    "set_correlation_id",
]

CORRELATION_HEADER = "X-Correlation-ID"
_WEBHOOK_ID_HEADER = "X-Shopify-Webhook-Id"


async def correlation_id_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    incoming = request.headers.get(CORRELATION_HEADER) or request.headers.get(_WEBHOOK_ID_HEADER)
    # Validate/normalise attacker-controlled input; invalid → fresh safe ID.
    cid = sanitize_correlation_id(incoming)
    token = correlation_id_var.set(cid)
    try:
        response = await call_next(request)
        response.headers[CORRELATION_HEADER] = cid
        return response
    finally:
        correlation_id_var.reset(token)
