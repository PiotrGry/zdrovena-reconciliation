"""Shipping API surface, split by responsibility (issue #313).

``webhooks.py`` was one 2000-line router covering Shopify ingestion, drafts,
execution, pickup, cancellation, labels, invoices, the DLQ and E2E test support.
Each of those is a separate reason to change the file, and a module that broad
invites logic being added inline "just here" rather than in the application layer.

URL paths, tags, response models and auth guards are unchanged: this splits the
file, not the API.
"""

from fastapi import APIRouter

from zdrovena.api.routers.shipping import (
    dlq,
    drafts,
    execution,
    fulfillment,
    ingestion,
    invoices,
    labels,
    test_support,
)

router = APIRouter()

# Order is presentational only — the exported schema is written with sorted keys.
for _sub in (
    ingestion.router,
    drafts.router,
    execution.router,
    fulfillment.router,
    labels.router,
    invoices.router,
    dlq.router,
    test_support.router,
):
    router.include_router(_sub)

__all__ = [
    "dlq",
    "drafts",
    "execution",
    "fulfillment",
    "ingestion",
    "invoices",
    "labels",
    "router",
    "test_support",
]
