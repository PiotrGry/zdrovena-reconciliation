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
    correlation_scope,
    get_correlation_id,
    sanitize_correlation_id,
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

    def test_oversized_incoming_id_is_replaced(self):
        oversized = "a" * 500
        res = _client().get("/health", headers={CORRELATION_HEADER: oversized})
        cid = res.headers.get(CORRELATION_HEADER)
        assert cid != oversized
        assert len(cid) == 12  # fresh generated hex

    def test_invalid_chars_incoming_id_is_replaced(self):
        # CRLF / space / control chars must never be echoed back into a header.
        res = _client().get("/health", headers={CORRELATION_HEADER: "bad id\twith spaces"})
        cid = res.headers.get(CORRELATION_HEADER)
        assert cid != "bad id\twith spaces"
        assert len(cid) == 12


class TestSanitizeCorrelationId:
    def test_valid_id_preserved(self):
        assert sanitize_correlation_id("abc-123_ID.9") == "abc-123_ID.9"

    def test_max_length_boundary_preserved(self):
        exactly_128 = "a" * 128
        assert sanitize_correlation_id(exactly_128) == exactly_128

    def test_oversized_replaced(self):
        assert sanitize_correlation_id("a" * 129) != "a" * 129

    def test_none_and_empty_generate_fresh(self):
        assert len(sanitize_correlation_id(None)) == 12
        assert len(sanitize_correlation_id("")) == 12
        assert len(sanitize_correlation_id("   ")) == 12

    def test_invalid_chars_replaced(self):
        for bad in ["a b", "a\nb", "a\tb", "a/b", "a;b", "<script>"]:
            assert sanitize_correlation_id(bad) != bad


class TestCorrelationScopeNoLeak:
    def test_scope_sets_and_resets(self):
        assert get_correlation_id() == "-"
        with correlation_scope("task-1") as cid:
            assert cid == "task-1"
            assert get_correlation_id() == "task-1"
        # after the block the context is restored — no leak to the next task
        assert get_correlation_id() == "-"

    def test_scope_resets_even_on_exception(self):
        import pytest

        with pytest.raises(RuntimeError):
            with correlation_scope("task-2"):
                assert get_correlation_id() == "task-2"
                raise RuntimeError("boom")
        assert get_correlation_id() == "-"

    def test_sequential_scopes_do_not_bleed(self):
        with correlation_scope("first"):
            assert get_correlation_id() == "first"
        # a second task that sets nothing must not see "first"
        assert get_correlation_id() == "-"
        with correlation_scope("second"):
            assert get_correlation_id() == "second"
        assert get_correlation_id() == "-"

    def test_scope_sanitizes_invalid_value(self):
        with correlation_scope("bad value with spaces") as cid:
            assert cid != "bad value with spaces"
            assert len(cid) == 12


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
