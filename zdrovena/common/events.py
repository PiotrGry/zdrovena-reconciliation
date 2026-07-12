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


def log_event(event: str, *, level: int = logging.INFO, **fields: Any) -> None:
    """Wyemituj ustrukturyzowane zdarzenie jako pojedynczy rekord JSON."""
    payload = {"event": event, "correlation_id": get_correlation_id(), **fields}
    _event_log.log(level, json.dumps(payload, default=str, ensure_ascii=False))
