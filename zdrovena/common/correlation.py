"""zdrovena.common.correlation — prymitywy correlation ID (bez zależności od web).

Sam identyfikator korelacji i filtr logów są niezależne od frameworka, więc
żyją w warstwie liściowej ``common`` (patrz reguły granic modułów w
``tests/fitness/test_module_boundaries.py``). Warstwa web (FastAPI middleware)
budowana jest na tym w ``zdrovena.api.observability``.
"""

from __future__ import annotations

import contextvars
import logging
import uuid

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
