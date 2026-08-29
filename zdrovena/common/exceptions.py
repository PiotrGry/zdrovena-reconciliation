"""
zdrovena.common.exceptions – Custom exception hierarchy
=========================================================
Replaces ad-hoc ``RuntimeError`` raises with typed exceptions so that
callers can catch specific failure categories.

Hierarchy::

    ZdrovenaError
    ├── MissingSecretError   — Keychain / credential lookup failure
    ├── APIError             — HTTP / REST API failure (Fakturownia, Zoho, KSeF)
    ├── PipelineAbortError   — Month-close pipeline abort (blockers, warnings gate)
    └── StorageUnavailableError — Azure Table Storage unreachable (NOT "no data")
"""

from __future__ import annotations

import logging

from zdrovena.common.events import log_event


class ZdrovenaError(Exception):
    """Base exception for all zdrovena errors."""


class MissingSecretError(ZdrovenaError):
    """Raised when a required secret cannot be resolved from any backing store."""

    def __init__(self, service: str, account: str = "") -> None:
        self.service = service
        self.account = account
        hint = f" (account={account!r})" if account else ""
        # Name every place the value could come from. The message is read from
        # container logs far more often than from a dev machine, where neither
        # a Keychain nor `zdrovena setup` exists.
        super().__init__(
            f"Missing secret: service={service!r}{hint}. "
            f"Set env var {service.upper().replace('-', '_')}, "
            f"or Key Vault secret {service.replace('_', '-')!r}, "
            f"or run: zdrovena setup"
        )


class APIError(ZdrovenaError):
    """Raised when an external API call fails after all retries."""

    def __init__(self, api: str, detail: str = "") -> None:
        self.api = api
        msg = f"{api} API error"
        if detail:
            msg += f": {detail}"
        super().__init__(msg)


class ApiResponseFormatError(ZdrovenaError):
    """Raised when an API response cannot be decoded as JSON."""

    def __init__(self, status_code: int, body_preview: str) -> None:
        self.status_code = status_code
        self.body_preview = body_preview
        super().__init__(f"Expected JSON but got status={status_code}: {body_preview}")


class PipelineAbortError(ZdrovenaError):
    """Raised when the month-close pipeline must abort (missing docs, warnings)."""

    def __init__(self, reason: str, blockers: list[str] | None = None) -> None:
        self.reason = reason
        self.blockers = blockers or []
        super().__init__(reason)


class StorageUnavailableError(ZdrovenaError):
    """Azure Table Storage could not answer - which is not the same as "no data".

    Read paths used to catch bare ``Exception`` and return ``None`` / ``[]``, so a
    timeout looked exactly like a record that is genuinely absent. That made an
    outage read as "the draft does not exist" and let a fingerprint lookup answer
    "no existing case", inviting a duplicate write (issue #310).
    """

    def __init__(self, store: str, operation: str, cause: BaseException) -> None:
        super().__init__(f"{store} storage unavailable during {operation}: {cause!r}")
        self.store = store
        self.operation = operation
        self.cause = cause


def storage_unavailable(
    store: str, operation: str, cause: BaseException
) -> StorageUnavailableError:
    """Build the error to raise, emitting the event alerting can key on.

    Telemetry lives here rather than at each of the six call sites so an outage
    always produces the same signal. Issue #214 wants operational alerts; an
    empty-list metric would be full of false positives, this is not.
    """
    log_event(
        "storage_unavailable",
        level=logging.ERROR,
        store=store,
        operation=operation,
        error_type=type(cause).__name__,
    )
    return StorageUnavailableError(store=store, operation=operation, cause=cause)
