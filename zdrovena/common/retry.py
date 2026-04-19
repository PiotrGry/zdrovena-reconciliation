"""
zdrovena.common.retry – Reusable retry-with-backoff utility
=============================================================
Provides a generic ``retry_request`` function that wraps HTTP calls with
exponential-backoff retry logic, jitter, and Retry-After support.

Previously, identical retry loops existed in:
  • FakturowniaClient._request()
  • KSeFClient._post_with_retry()

Now both delegate to this single implementation.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any, Callable, TypeVar

import requests

from zdrovena.common.config import DEFAULT_RETRY_COUNT, DEFAULT_RETRY_DELAY

logger = logging.getLogger("zdrovena.common.retry")

T = TypeVar("T")

# Status codes that trigger a retry with optional Retry-After header
_RETRYABLE_STATUS_CODES = frozenset({429, 503})


def _jittered_delay(base: float, jitter_ratio: float = 0.2) -> float:
    """Return *base* with ±jitter_ratio random variation."""
    low = base * (1 - jitter_ratio)
    high = base * (1 + jitter_ratio)
    return random.uniform(low, high)


def retry_request(
    session: requests.Session,
    method: str,
    url: str,
    *,
    max_retries: int = DEFAULT_RETRY_COUNT,
    initial_delay: float = DEFAULT_RETRY_DELAY,
    timeout: int = 30,
    caller: str = "",
    sleep_fn: Callable[[float], None] | None = None,
    **kwargs: Any,
) -> requests.Response:
    """
    Execute an HTTP request with exponential-backoff retry.

    Parameters
    ----------
    session       : ``requests.Session`` to use.
    method        : HTTP method (``GET``, ``POST``, …).
    url           : Full URL.
    max_retries   : How many attempts before giving up.
    initial_delay : Seconds to wait after the first failure (doubles each retry).
    timeout       : Per-request timeout in seconds.
    caller        : Label for log messages (e.g. ``"Fakturownia"``).
    sleep_fn      : Injectable sleep function (defaults to ``time.sleep``).
                    Useful for testing without actual delays.
    **kwargs      : Extra keyword arguments forwarded to ``session.request()``.

    Returns
    -------
    requests.Response

    Raises
    ------
    RuntimeError
        After *max_retries* consecutive failures.
    """
    _sleep = sleep_fn or time.sleep
    delay = initial_delay
    last_exc: Exception | None = None

    for attempt in range(1, max_retries + 1):
        try:
            resp = session.request(method, url, timeout=timeout, **kwargs)
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            last_exc = exc
            tag = f" ({caller})" if caller else ""
            logger.warning(
                "Request failed%s (attempt %d/%d): %s",
                tag, attempt, max_retries, exc,
            )
            if attempt < max_retries:
                wait = delay
                # Respect Retry-After header on 429 / 503
                resp_obj = getattr(exc, "response", None)
                if resp_obj is not None:
                    status = getattr(resp_obj, "status_code", None)
                    if status in _RETRYABLE_STATUS_CODES:
                        retry_after = resp_obj.headers.get("Retry-After")
                        if retry_after:
                            try:
                                wait = max(float(retry_after), wait)
                            except (ValueError, TypeError):
                                pass
                wait = _jittered_delay(wait)
                _sleep(wait)
                delay *= 2

    raise RuntimeError(
        f"{caller + ': ' if caller else ''}HTTP request failed after "
        f"{max_retries} attempts: {last_exc}"
    )
