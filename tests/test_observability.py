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


class TestCorrelationIdValidation:
    """R4-B: walidacja długości/znaków przychodzącego correlation ID."""

    def test_oversized_incoming_id_is_replaced(self):
        res = _client().get("/health", headers={CORRELATION_HEADER: "x" * 200})
        cid = res.headers.get(CORRELATION_HEADER)
        assert cid and cid != "x" * 200
        assert len(cid) == 12  # wygenerowany hex

    def test_invalid_characters_are_replaced(self):
        res = _client().get("/health", headers={CORRELATION_HEADER: "abc def{}"})
        cid = res.headers.get(CORRELATION_HEADER)
        assert cid and cid != "abc def{}"
        assert len(cid) == 12

    def test_valid_id_with_allowed_separators_preserved(self):
        res = _client().get("/health", headers={CORRELATION_HEADER: "req-1.2_ok"})
        assert res.headers.get(CORRELATION_HEADER) == "req-1.2_ok"

    def test_set_correlation_id_sanitizes_invalid_value(self):
        from zdrovena.api.observability import reset_correlation_id

        token = set_correlation_id("bad id\nwith newline")
        try:
            cid = get_correlation_id()
            assert "\n" not in cid and " " not in cid
            assert len(cid) == 12
        finally:
            reset_correlation_id(token)


class TestBackgroundContextReset:
    """R4-B: token/reset w finally — kontekst nie wycieka między zadaniami tła."""

    def test_create_draft_safely_resets_context(self):
        from zdrovena.api.routers.webhooks import _create_draft_safely

        class _Store:
            def enqueue_dlq(self, **kwargs):  # pragma: no cover - nie powinno być wywołane
                raise AssertionError("DLQ nieoczekiwane dla pustego zamówienia")

        before = get_correlation_id()
        # _create_draft rzuci na pustym zamówieniu → ścieżka DLQ; Store.enqueue_dlq
        # podnosi AssertionError, który jest łapany przez wewnętrzny try/except.
        _create_draft_safely({}, _Store(), None, correlation_id="cid-bg-1")
        assert get_correlation_id() == before  # kontekst przywrócony po zadaniu
