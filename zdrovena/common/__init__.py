"""
zdrovena.common – Shared Fakturownia API Client & utilities.

Usage::

    from zdrovena.common import FakturowniaClient

    client = FakturowniaClient.from_keyring()
    invoices = client.fetch_invoices("2025-01-01", "2025-01-31", income="yes")
"""

from zdrovena.common.client import FakturowniaClient
from zdrovena.common.config import (
    DEFAULT_DOMAIN,
    KEYCHAIN_ACCOUNT,
    KEYCHAIN_SERVICE,
)
from zdrovena.common.exceptions import (
    APIError,
    ApiResponseFormatError,
    MissingSecretError,
    PipelineAbortError,
    ZdrovenaError,
)

__all__ = [
    "FakturowniaClient",
    "DEFAULT_DOMAIN",
    "KEYCHAIN_ACCOUNT",
    "KEYCHAIN_SERVICE",
    "ZdrovenaError",
    "MissingSecretError",
    "APIError",
    "ApiResponseFormatError",
    "PipelineAbortError",
]
