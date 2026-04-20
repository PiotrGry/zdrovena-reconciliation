"""Tests for zdrovena.common.exceptions."""

from __future__ import annotations

import pytest

from zdrovena.common.exceptions import (
    APIError,
    ApiResponseFormatError,
    MissingSecretError,
    PipelineAbortError,
    ZdrovenaError,
)

# ── Hierarchy ─────────────────────────────────────────────────────────────────


class TestHierarchy:
    def test_all_inherit_from_zdrovena_error(self):
        assert issubclass(MissingSecretError, ZdrovenaError)
        assert issubclass(APIError, ZdrovenaError)
        assert issubclass(ApiResponseFormatError, ZdrovenaError)
        assert issubclass(PipelineAbortError, ZdrovenaError)

    def test_all_are_exceptions(self):
        assert issubclass(ZdrovenaError, Exception)

    def test_can_catch_with_base_class(self):
        with pytest.raises(ZdrovenaError):
            raise MissingSecretError("svc")


# ── MissingSecretError ────────────────────────────────────────────────────────


class TestMissingSecretError:
    def test_message_with_account(self):
        exc = MissingSecretError("my_service", "my_account")
        assert "my_service" in str(exc)
        assert "my_account" in str(exc)
        assert "zdrovena setup" in str(exc)

    def test_message_without_account(self):
        exc = MissingSecretError("my_service")
        assert "my_service" in str(exc)
        assert "account" not in str(exc)

    def test_attrs(self):
        exc = MissingSecretError("svc", "acct")
        assert exc.service == "svc"
        assert exc.account == "acct"


# ── APIError ──────────────────────────────────────────────────────────────────


class TestAPIError:
    def test_message_without_detail(self):
        exc = APIError("Fakturownia")
        assert str(exc) == "Fakturownia API error"
        assert exc.api == "Fakturownia"

    def test_message_with_detail(self):
        exc = APIError("KSeF", "timeout after 30s")
        assert str(exc) == "KSeF API error: timeout after 30s"


# ── PipelineAbortError ────────────────────────────────────────────────────────


class TestPipelineAbortError:
    def test_reason(self):
        exc = PipelineAbortError("missing docs")
        assert str(exc) == "missing docs"
        assert exc.reason == "missing docs"
        assert exc.blockers == []

    def test_blockers(self):
        exc = PipelineAbortError("blocked", ["no bank stmt", "no JPK"])
        assert exc.blockers == ["no bank stmt", "no JPK"]


# ── ApiResponseFormatError ────────────────────────────────────────────────────


class TestApiResponseFormatError:
    def test_message(self):
        exc = ApiResponseFormatError(200, "<html>oops</html>")
        assert "status=200" in str(exc)
        assert "<html>oops</html>" in str(exc)

    def test_attrs(self):
        exc = ApiResponseFormatError(502, "bad gateway")
        assert exc.status_code == 502
        assert exc.body_preview == "bad gateway"

    def test_inherits_from_zdrovena_error(self):
        with pytest.raises(ZdrovenaError):
            raise ApiResponseFormatError(500, "err")
