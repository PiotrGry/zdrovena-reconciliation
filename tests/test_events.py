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


class TestPiiMasking:
    def test_high_risk_fields_are_masked(self, caplog):
        with caplog.at_level(logging.INFO, logger="zdrovena.events"):
            log_event(
                "draft.created",
                order_number="1001",
                email="jan.kowalski@example.com",
                phone="+48123456789",
                customer_name="Jan Kowalski",
            )
        events = _parse_events(caplog)
        payload = events[0]
        assert payload["order_number"] == "1001"  # nie-PII zostaje jawne
        assert payload["email"] == "j***"
        assert payload["phone"] == "+***"
        assert payload["customer_name"] == "J***"
        raw = json.dumps(payload)
        assert "jan.kowalski@example.com" not in raw
        assert "123456789" not in raw
        assert "Kowalski" not in raw

    def test_nested_dict_and_list_fields_are_masked(self, caplog):
        with caplog.at_level(logging.INFO, logger="zdrovena.events"):
            log_event(
                "sync.completed",
                shopify={"orders": [{"email": "a@b.pl", "street": "Polna 1", "qty": 2}]},
            )
        events = _parse_events(caplog)
        order = events[0]["shopify"]["orders"][0]
        assert order["email"] == "a***"
        assert order["street"] == "P***"
        assert order["qty"] == 2

    def test_masking_is_case_insensitive_and_skips_empty(self, caplog):
        with caplog.at_level(logging.INFO, logger="zdrovena.events"):
            log_event("draft.created", Email="x@y.pl", phone="", draft_id="d9")
        events = _parse_events(caplog)
        assert events[0]["Email"] == "x***"
        assert events[0]["phone"] == ""  # puste pole nie wymaga maski
        assert events[0]["draft_id"] == "d9"
