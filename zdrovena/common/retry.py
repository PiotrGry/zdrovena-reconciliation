"""
zdrovena.common.retry – Reusable retry-with-backoff utility
=============================================================
Provides a generic ``retry_request`` function and a ``@with_retry``
decorator that wraps HTTP calls with exponential-backoff retry logic.

Previously, identical retry loops existed in:
  • FakturowniaClient._request()
  • KSeFClient._post_with_retry()

Now both delegate to this single implementation.
"""

from __future__ import annotations

import logging
import time
from typing import TypeVar

import requests

from zdrovena.common.config import DEFAULT_RETRY_COUNT, DEFAULT_RETRY_DELAY

logger = logging.getLogger("zdrovena.common.retry")

T = TypeVar("T")


def retry_request(
    session: requests.Session,
    method: str,
    url: str,
    *,
    max_retries: int = DEFAULT_RETRY_COUNT,
    initial_delay: float = DEFAULT_RETRY_DELAY,
    timeout: int = 30,
    caller: str = "",
    **kwargs: object,
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
    **kwargs      : Extra keyword arguments forwarded to ``session.request()``.

    Returns
    -------
    requests.Response

    Raises
    ------
    RuntimeError
        After *max_retries* consecutive failures.
    """
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
                time.sleep(delay)
                delay *= 2

    raise RuntimeError(
        f"{caller + ': ' if caller else ''}HTTP request failed after "
        f"{max_retries} attempts: {last_exc}"
    )
