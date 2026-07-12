"""Testy log_event: kształt JSON + zdarzenia sukcesu (draft.created, shipment.created)."""

from __future__ import annotations

import json
import logging

from zdrovena.api.observability import correlation_id_var, set_correlation_id
from zdrovena.common.events import log_event


def _parse_events(caplog) -> list[dict]:
    out = []
    for rec in caplog.records:
        if rec.name == "zdrovena.events":
            out.append(json.loads(rec.getMessage()))
    return out


class TestLogEvent:
    def test_emits_json_with_event_and_correlation_id(self, caplog):
        token = set_correlation_id("cid-evt-1")
        try:
            with caplog.at_level(logging.INFO, logger="zdrovena.events"):
                log_event("draft.created", order_number="1001", draft_id="d1")
        finally:
            correlation_id_var.reset(token)

        events = _parse_events(caplog)
        assert len(events) == 1
        assert events[0]["event"] == "draft.created"
        assert events[0]["correlation_id"] == "cid-evt-1"
        assert events[0]["order_number"] == "1001"
        assert events[0]["draft_id"] == "d1"

    def test_non_serializable_field_is_stringified(self, caplog):
        with caplog.at_level(logging.INFO, logger="zdrovena.events"):
            log_event("sync.completed", allegro=object())
        events = _parse_events(caplog)
        assert events[0]["event"] == "sync.completed"
        # default=str → obiekt zserializowany jako string, nie wyjątek
        assert isinstance(events[0]["allegro"], str)
