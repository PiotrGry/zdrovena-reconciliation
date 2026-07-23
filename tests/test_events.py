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
    def test_top_level_pii_fields_masked(self, caplog):
        with caplog.at_level(logging.INFO, logger="zdrovena.events"):
            log_event(
                "draft.created",
                order_number="1001",
                actor="operator@example.com",
                email="jan@example.com",
                phone="600100200",
                first_name="Jan",
                last_name="Kowalski",
            )
        e = _parse_events(caplog)[0]
        # operational, non-identifying → kept
        assert e["order_number"] == "1001"
        # high-risk PII → masked
        assert e["actor"] == "***"
        assert e["email"] == "***"
        assert e["phone"] == "***"
        assert e["first_name"] == "***"
        assert e["last_name"] == "***"

    def test_nested_pii_fields_masked(self, caplog):
        with caplog.at_level(logging.INFO, logger="zdrovena.events"):
            log_event(
                "shipment.created",
                draft_id="d1",
                receiver={
                    "receiver_name": "Anna Nowak",
                    "receiver_phone": "700200300",
                    "city": "Kraków",
                    "address": {"street": "Krakowska", "building_number": "24"},
                },
            )
        e = _parse_events(caplog)[0]
        assert e["draft_id"] == "d1"
        assert e["receiver"]["receiver_name"] == "***"
        assert e["receiver"]["receiver_phone"] == "***"
        # city is not high-risk → kept for operational queries
        assert e["receiver"]["city"] == "Kraków"
        # nested address dict is itself under a PII key → whole subtree masked
        assert e["receiver"]["address"] == "***"

    def test_pii_key_matching_is_case_insensitive(self, caplog):
        with caplog.at_level(logging.INFO, logger="zdrovena.events"):
            log_event("buyer.seen", Email="X@y.pl", NIP="1234567890")
        e = _parse_events(caplog)[0]
        assert e["Email"] == "***"
        assert e["NIP"] == "***"

    def test_pii_inside_list_masked(self, caplog):
        with caplog.at_level(logging.INFO, logger="zdrovena.events"):
            log_event("order.items", items=[{"name": "Woda", "email": "a@b.pl"}])
        e = _parse_events(caplog)[0]
        assert e["items"][0]["name"] == "***"  # "name" is a PII key
        assert e["items"][0]["email"] == "***"
