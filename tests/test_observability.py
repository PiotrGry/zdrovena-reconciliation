"""Testy correlation ID: middleware, filtr logów, koperta błędu, propagacja do tła."""

from __future__ import annotations

import logging
import os

os.environ.setdefault("AZURE_AUTH_DISABLED", "true")

from fastapi.testclient import TestClient

from zdrovena.api.main import app
from zdrovena.api.observability import (
    CORRELATION_HEADER,
    CorrelationIdFilter,
    correlation_id_var,
    get_correlation_id,
    set_correlation_id,
)


def _client() -> TestClient:
    return TestClient(app, raise_server_exceptions=True)


class TestMiddleware:
    def test_generates_id_when_absent(self):
        res = _client().get("/health")
        assert res.status_code == 200
        cid = res.headers.get(CORRELATION_HEADER)
        assert cid and cid != "-"
        # domyślnie generujemy 12-znakowy hex
        assert len(cid) == 12

    def test_preserves_incoming_id(self):
        res = _client().get("/health", headers={CORRELATION_HEADER: "moje-id-123"})
        assert res.headers.get(CORRELATION_HEADER) == "moje-id-123"

    def test_falls_back_to_shopify_webhook_id(self):
        res = _client().get("/health", headers={"X-Shopify-Webhook-Id": "wh-999"})
        assert res.headers.get(CORRELATION_HEADER) == "wh-999"

    def test_resets_context_after_request(self):
        _client().get("/health", headers={CORRELATION_HEADER: "abc"})
        # poza żądaniem contextvar wraca do wartości domyślnej
        assert correlation_id_var.get() == "-"


class TestLogFilter:
    def test_filter_sets_attribute(self):
        token = set_correlation_id("cid-log-1")
        try:
            record = logging.LogRecord("t", logging.INFO, __file__, 1, "msg", None, None)
            assert CorrelationIdFilter().filter(record) is True
            assert record.correlation_id == "cid-log-1"
        finally:
            correlation_id_var.reset(token)


class TestErrorEnvelope:
    def test_envelope_carries_real_correlation_id(self):
        from fastapi import FastAPI

        from zdrovena.api.errors import install_exception_handlers
        from zdrovena.api.observability import correlation_id_middleware
        from zdrovena.common.shipping_exceptions import InPostLockerUnavailableError

        mini = FastAPI()
        mini.middleware("http")(correlation_id_middleware)
        install_exception_handlers(mini)

        @mini.get("/boom")
        def _boom() -> dict:
            raise InPostLockerUnavailableError("locker full")

        res = TestClient(mini, raise_server_exceptions=False).get(
            "/boom", headers={CORRELATION_HEADER: "cid-err-1"}
        )
        body = res.json()
        assert body["error_code"] == "InPostLockerUnavailableError"
        assert body["message_pl"]
        assert body["correlation_id"] == "cid-err-1"


class TestContextHelpers:
    def test_get_default_outside_request(self):
        assert get_correlation_id() == "-"

    def test_set_empty_becomes_dash(self):
        token = set_correlation_id("")
        try:
            assert get_correlation_id() == "-"
        finally:
            correlation_id_var.reset(token)
