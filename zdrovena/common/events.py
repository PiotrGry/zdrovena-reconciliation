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

_MASK = "***"

# R4-B: high-risk PII field names. Structured events land in Log Analytics, which
# is broadly readable, so customer-identifying values must never be emitted in
# clear. Keys are matched case-insensitively, at any nesting depth. Low-risk
# routing fields (order_number, draft_id, city, post_code, counts) are kept —
# they are needed for operational queries and are not directly identifying.
_PII_KEYS = frozenset(
    {
        "email",
        "buyer_email",
        "receiver_email",
        "phone",
        "phone_number",
        "buyer_phone",
        "receiver_phone",
        "first_name",
        "last_name",
        "name",
        "receiver_name",
        "buyer_name",
        "customer_name",
        "address",
        "address1",
        "address2",
        "street",
        "building_number",
        "flat_number",
        "nip",
        "pesel",
        "tax_id",
        # Operator identity may currently be an e-mail address. Prefer a
        # separate opaque `actor_id` for correlation; never export `actor`.
        "actor",
    }
)


def _mask_pii(value: Any) -> Any:
    """Recursively replace values under high-risk PII keys with ``***``."""
    if isinstance(value, dict):
        return {
            k: (_MASK if isinstance(k, str) and k.lower() in _PII_KEYS else _mask_pii(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_mask_pii(v) for v in value]
    return value


def log_event(event: str, *, level: int = logging.INFO, **fields: Any) -> None:
    """Wyemituj ustrukturyzowane zdarzenie jako pojedynczy rekord JSON.

    Pola pod kluczami PII (patrz ``_PII_KEYS``) są maskowane na każdym poziomie
    zagnieżdżenia zanim trafią do logu (R4-B).
    """
    masked = {k: (_MASK if k.lower() in _PII_KEYS else _mask_pii(v)) for k, v in fields.items()}
    payload = {"event": event, "correlation_id": get_correlation_id(), **masked}
    _event_log.log(level, json.dumps(payload, default=str, ensure_ascii=False))
