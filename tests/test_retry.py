"""Tests for zdrovena.common.retry."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests

from zdrovena.common.retry import retry_request


class TestRetryRequestSuccess:
    def test_first_attempt_success(self):
        session = MagicMock(spec=requests.Session)
        mock_resp = MagicMock(spec=requests.Response)
        mock_resp.raise_for_status = MagicMock()
        session.request.return_value = mock_resp

        result = retry_request(session, "GET", "https://example.com/api")

        assert result is mock_resp
        session.request.assert_called_once()

    def test_passes_kwargs_to_session(self):
        session = MagicMock(spec=requests.Session)
        mock_resp = MagicMock(spec=requests.Response)
        mock_resp.raise_for_status = MagicMock()
        session.request.return_value = mock_resp

        retry_request(
            session,
            "POST",
            "https://example.com/api",
            json={"key": "value"},
            headers={"X-Custom": "yes"},
            timeout=10,
        )

        call_kwargs = session.request.call_args
        assert call_kwargs[1]["json"] == {"key": "value"}
        assert call_kwargs[1]["headers"] == {"X-Custom": "yes"}


class TestRetryRequestFailure:
    def test_retries_on_request_exception(self):
        session = MagicMock(spec=requests.Session)
        session.request.side_effect = requests.ConnectionError("refused")

        with pytest.raises(RuntimeError, match="HTTP request failed after 3 attempts"):
            retry_request(
                session,
                "GET",
                "https://example.com/api",
                max_retries=3,
                initial_delay=0.1,
                sleep_fn=lambda _: None,
            )

        assert session.request.call_count == 3

    def test_retries_on_http_error(self):
        session = MagicMock(spec=requests.Session)
        mock_resp = MagicMock(spec=requests.Response)
        mock_resp.status_code = 500
        mock_resp.raise_for_status.side_effect = requests.HTTPError("500 Server Error")
        session.request.return_value = mock_resp

        with pytest.raises(RuntimeError):
            retry_request(
                session,
                "GET",
                "https://example.com/api",
                max_retries=2,
                initial_delay=0.01,
                sleep_fn=lambda _: None,
            )

        assert session.request.call_count == 2

    def test_exponential_backoff_with_jitter(self):
        """Delays should follow exponential backoff ±20% jitter."""
        session = MagicMock(spec=requests.Session)
        session.request.side_effect = requests.ConnectionError("refused")
        sleeps: list[float] = []

        with pytest.raises(RuntimeError):
            retry_request(
                session,
                "GET",
                "https://example.com/api",
                max_retries=4,
                initial_delay=1.0,
                sleep_fn=sleeps.append,
            )

        # Should sleep between attempts: ~1.0, ~2.0, ~4.0 (not after last)
        assert len(sleeps) == 3
        assert 0.8 <= sleeps[0] <= 1.2  # 1.0 ± 20%
        assert 1.6 <= sleeps[1] <= 2.4  # 2.0 ± 20%
        assert 3.2 <= sleeps[2] <= 4.8  # 4.0 ± 20%

    def test_succeeds_after_retries(self):
        session = MagicMock(spec=requests.Session)
        fail_resp = MagicMock(spec=requests.Response)
        fail_resp.raise_for_status.side_effect = requests.HTTPError("503")

        ok_resp = MagicMock(spec=requests.Response)
        ok_resp.raise_for_status = MagicMock()

        session.request.side_effect = [
            requests.ConnectionError("refused"),
            ok_resp,
        ]

        result = retry_request(
            session,
            "GET",
            "https://example.com/api",
            max_retries=3,
            initial_delay=0.01,
            sleep_fn=lambda _: None,
        )

        assert result is ok_resp
        assert session.request.call_count == 2


class TestRetryRequestCaller:
    def test_caller_in_error_message(self):
        session = MagicMock(spec=requests.Session)
        session.request.side_effect = requests.ConnectionError("refused")

        with pytest.raises(RuntimeError, match="Fakturownia"):
            retry_request(
                session,
                "GET",
                "https://example.com/api",
                max_retries=1,
                caller="Fakturownia",
            )

    def test_no_caller_in_error_message(self):
        session = MagicMock(spec=requests.Session)
        session.request.side_effect = requests.ConnectionError("refused")

        with pytest.raises(RuntimeError, match=r"^HTTP request failed"):
            retry_request(
                session,
                "GET",
                "https://example.com/api",
                max_retries=1,
                caller="",
            )


class TestRetryAfterHeader:
    def test_429_with_retry_after(self):
        """Retry-After header on 429 should override the exponential delay."""
        session = MagicMock(spec=requests.Session)
        mock_resp = MagicMock(spec=requests.Response)
        mock_resp.status_code = 429
        mock_resp.headers = {"Retry-After": "10"}
        exc = requests.HTTPError("429 Too Many Requests", response=mock_resp)
        mock_resp.raise_for_status.side_effect = exc
        session.request.return_value = mock_resp

        sleeps: list[float] = []

        with pytest.raises(RuntimeError):
            retry_request(
                session,
                "GET",
                "https://example.com/api",
                max_retries=2,
                initial_delay=1.0,
                sleep_fn=sleeps.append,
            )

        # Should use Retry-After=10 (> initial_delay=1.0), with ±20% jitter
        assert len(sleeps) == 1
        assert 8.0 <= sleeps[0] <= 12.0  # 10 ± 20%

    def test_503_with_retry_after(self):
        """503 should also respect Retry-After."""
        session = MagicMock(spec=requests.Session)
        mock_resp = MagicMock(spec=requests.Response)
        mock_resp.status_code = 503
        mock_resp.headers = {"Retry-After": "5"}
        exc = requests.HTTPError("503 Service Unavailable", response=mock_resp)
        mock_resp.raise_for_status.side_effect = exc
        session.request.return_value = mock_resp

        sleeps: list[float] = []

        with pytest.raises(RuntimeError):
            retry_request(
                session,
                "GET",
                "https://example.com/api",
                max_retries=2,
                initial_delay=1.0,
                sleep_fn=sleeps.append,
            )

        assert len(sleeps) == 1
        assert 4.0 <= sleeps[0] <= 6.0  # 5 ± 20%

    def test_429_without_retry_after_uses_backoff(self):
        """429 without Retry-After should use normal exponential backoff."""
        session = MagicMock(spec=requests.Session)
        mock_resp = MagicMock(spec=requests.Response)
        mock_resp.status_code = 429
        mock_resp.headers = {}
        exc = requests.HTTPError("429", response=mock_resp)
        mock_resp.raise_for_status.side_effect = exc
        session.request.return_value = mock_resp

        sleeps: list[float] = []

        with pytest.raises(RuntimeError):
            retry_request(
                session,
                "GET",
                "https://example.com/api",
                max_retries=2,
                initial_delay=2.0,
                sleep_fn=sleeps.append,
            )

        assert len(sleeps) == 1
        assert 1.6 <= sleeps[0] <= 2.4  # 2.0 ± 20%


class TestSleepFnInjection:
    def test_custom_sleep_fn_is_used(self):
        session = MagicMock(spec=requests.Session)
        session.request.side_effect = requests.ConnectionError("refused")

        calls: list[float] = []

        with pytest.raises(RuntimeError):
            retry_request(
                session,
                "GET",
                "https://example.com/api",
                max_retries=2,
                initial_delay=1.0,
                sleep_fn=calls.append,
            )

        assert len(calls) == 1
        assert isinstance(calls[0], float)
