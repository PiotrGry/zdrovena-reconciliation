"""zdrovena.common.events — ustrukturyzowane logowanie zdarzeń (log_event).

Dziś logowane są głównie porażki; sukcesy tła (draft utworzony, przesyłka
nadana, sync zakończony) znikają bez śladu, więc nie da się policzyć wolumenu
udanych operacji w Log Analytics (PR-9 z master-planu).

``log_event`` emituje jeden rekord JSON na logger ``zdrovena.events`` z polami
``event`` + ``correlation_id`` (z observability, PR-8) + dowolnymi metadanymi.
JSON jest parsowalny w KQL (``parse_json(...)``).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from zdrovena.common.correlation import get_correlation_id

_event_log = logging.getLogger("zdrovena.events")

#: Pola wysokiego ryzyka PII maskowane w zdarzeniach strukturalnych (R4-B).
#: Dopasowanie po nazwie pola (case-insensitive), również w zagnieżdżonych dict.
_PII_FIELD_NAMES = frozenset(
    {
        "email",
        "phone",
        "telephone",
        "customer_name",
        "first_name",
        "last_name",
        "name",
        "address",
        "address1",
        "address2",
        "street",
        "city",
        "zip",
        "postal_code",
        "company",
        "nip",
        "pesel",
    }
)

_MASK = "***"


def _mask_value(value: Any) -> str:
    """Zamaskuj wartość PII: zostaw pierwszy znak, resztę zastąp ``***``."""
    text = str(value)
    if not text:
        return _MASK
    return f"{text[0]}{_MASK}"


def _mask_pii(value: Any) -> Any:
    """Rekurencyjnie zamaskuj pola PII w dict/list; skalar zwracany bez zmian."""
    if isinstance(value, dict):
        return {
            k: (_mask_value(v) if str(k).lower() in _PII_FIELD_NAMES and v else _mask_pii(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_mask_pii(item) for item in value]
    return value


def log_event(event: str, *, level: int = logging.INFO, **fields: Any) -> None:
    """Wyemituj ustrukturyzowane zdarzenie jako pojedynczy rekord JSON.

    Pola wysokiego ryzyka PII (e-mail, telefon, nazwisko, adres…) są maskowane
    przed serializacją — logi trafiają do Log Analytics i nie mogą zawierać
    danych osobowych klientów w postaci jawnej.
    """
    payload = {"event": event, "correlation_id": get_correlation_id(), **_mask_pii(fields)}
    _event_log.log(level, json.dumps(payload, default=str, ensure_ascii=False))
