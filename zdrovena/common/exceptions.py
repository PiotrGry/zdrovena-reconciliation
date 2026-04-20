"""
zdrovena.common.exceptions – Custom exception hierarchy
=========================================================
Replaces ad-hoc ``RuntimeError`` raises with typed exceptions so that
callers can catch specific failure categories.

Hierarchy::

    ZdrovenaError
    ├── MissingSecretError   — Keychain / credential lookup failure
    ├── APIError             — HTTP / REST API failure (Fakturownia, Zoho, KSeF)
    └── PipelineAbortError   — Month-close pipeline abort (blockers, warnings gate)
"""

from __future__ import annotations


class ZdrovenaError(Exception):
    """Base exception for all zdrovena errors."""


class MissingSecretError(ZdrovenaError):
    """Raised when a required secret is not found in the macOS Keychain."""

    def __init__(self, service: str, account: str = "") -> None:
        self.service = service
        self.account = account
        hint = f" (account={account!r})" if account else ""
        super().__init__(
            f"Missing secret in Keychain: service={service!r}{hint}. Run: zdrovena setup"
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
