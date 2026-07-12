"""zdrovena.common.correlation — prymitywy correlation ID (bez zależności od web).

Sam identyfikator korelacji i filtr logów są niezależne od frameworka, więc
żyją w warstwie liściowej ``common`` (patrz reguły granic modułów w
``tests/fitness/test_module_boundaries.py``). Warstwa web (FastAPI middleware)
budowana jest na tym w ``zdrovena.api.observability``.
"""

from __future__ import annotations

import contextvars
import logging
import re
import uuid

correlation_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default="-"
)

#: Maksymalna akceptowana długość przychodzącego correlation ID.
MAX_CORRELATION_ID_LENGTH = 64

#: Dozwolone znaki: alfanumeryczne plus separator ``-``, ``_``, ``.``.
#: Chroni logi/nagłówki przed wstrzyknięciem (CRLF, znaki sterujące, unicode).
_VALID_CORRELATION_ID = re.compile(r"^[A-Za-z0-9._-]+$")


def is_valid_correlation_id(value: str) -> bool:
    """True, gdy ``value`` jest bezpiecznym correlation ID (długość + znaki)."""
    return 0 < len(value) <= MAX_CORRELATION_ID_LENGTH and bool(_VALID_CORRELATION_ID.match(value))


def sanitize_correlation_id(value: str) -> str:
    """Zwróć ``value`` gdy poprawne; w przeciwnym razie wygeneruj bezpieczne ID.

    Puste/nieprawidłowe (za długie, niedozwolone znaki) wejście nigdy nie trafia
    do logów ani nagłówków odpowiedzi — zastępujemy je świeżym identyfikatorem.
    """
    value = value.strip()
    if is_valid_correlation_id(value):
        return value
    return new_correlation_id()


def get_correlation_id() -> str:
    """Zwróć correlation ID bieżącego kontekstu (``"-"`` poza żądaniem)."""
    return correlation_id_var.get()


def set_correlation_id(value: str) -> contextvars.Token[str]:
    """Ustaw correlation ID (używane przez zadania tła). Puste → ``"-"``.

    Nieprawidłowe wartości (za długie, niedozwolone znaki) są zastępowane
    bezpiecznym wygenerowanym ID. Zwrócony token przekaż do
    :func:`reset_correlation_id` w bloku ``finally``, aby kontekst nie
    wyciekał między zadaniami.
    """
    value = (value or "").strip()
    if not value or value == "-":
        return correlation_id_var.set("-")
    return correlation_id_var.set(sanitize_correlation_id(value))


def reset_correlation_id(token: contextvars.Token[str]) -> None:
    """Przywróć poprzedni correlation ID (para do :func:`set_correlation_id`)."""
    correlation_id_var.reset(token)


def new_correlation_id() -> str:
    return uuid.uuid4().hex[:12]


class CorrelationIdFilter(logging.Filter):
    """Dokleja ``correlation_id`` do każdego rekordu logu przechodzącego handler."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = correlation_id_var.get()
        return True
