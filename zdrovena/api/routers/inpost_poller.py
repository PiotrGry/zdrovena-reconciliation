"""zdrovena.api.routers.inpost_poller — resolve InPost shipments awaiting a waybill.

ShipX creation is asynchronous: the POST returns a shipment id, and the tracking
number appears only once InPost confirms the parcel. ``_run_inpost`` therefore
leaves the draft at ``pending_confirmation`` and something has to come back for
the number.

Until now the only thing that did was the browser: a 5 second ``setInterval`` in
ShippingView, plus the operator's "Sprawdź status" button. Execute a batch, close
the tab, and those drafts sat unresolved until somebody opened the page again.
This module is the server-side half, run from the same scheduled cycle as the
Allegro poller so tracking resolves whether or not anyone is watching.

Resolution goes through ``resume_inpost_shipment``, the same path the operator's
button and a retry use, so the three can never disagree about what the shipment
is or send a second POST for it.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from zdrovena.api.routers.webhooks import (
    SHIPMENT_ORIGIN_SYSTEM,
    _emit_tracking_assigned,
    _shipment_patch,
)
from zdrovena.common.secrets import get_secret
from zdrovena.shipping.providers.inpost import (
    is_resumable_inpost_draft,
    resume_inpost_shipment,
)

logger = logging.getLogger("zdrovena.api.routers.inpost_poller")


def _mock_courier() -> bool:
    """Read at call time, not import time, so tests and dev can toggle it."""
    return os.environ.get("MOCK_COURIER", "").strip() == "1"


def resolve_pending_inpost_once(
    *,
    shipping_store: Any,
    client: Any = None,
) -> dict[str, int]:
    """One resolution cycle over drafts stuck at ``pending_confirmation``.

    Returns per-cycle stats. Never raises: this runs inside a scheduled job that
    must survive a bad draft or a courier outage.
    """
    stats = {"scanned": 0, "resolved": 0, "still_pending": 0, "errors": 0}

    if _mock_courier():
        logger.info("MOCK_COURIER: skipping InPost pending resolution")
        return stats

    try:
        # High limit for the same reason the other sync paths use one: the
        # default 200 hides the oldest rows, and a draft stuck pending is
        # exactly the kind that has been sitting around long enough to fall out
        # of that window.
        drafts = shipping_store.list_drafts(limit=10_000)
    except Exception:
        # Resilience boundary: a store read failure must not abort the cycle.
        logger.exception("shipping_store.list_drafts failed")
        stats["errors"] += 1
        return stats

    pending = [
        draft
        for draft in drafts
        if draft.get("courier") == "inpost" and is_resumable_inpost_draft(draft)
    ]
    stats["scanned"] = len(pending)
    if not pending:
        return stats

    if client is None:
        try:
            from zdrovena.common.inpost import InPostClient

            client = InPostClient(
                get_secret("inpost_api_token"),
                get_secret("inpost_organization_id"),
            )
        except Exception:
            logger.exception("InPost credentials unavailable — cannot resolve pending shipments")
            stats["errors"] += 1
            return stats

    for draft in pending:
        draft_id = str(draft.get("id") or "")
        try:
            patch = resume_inpost_shipment(
                client,
                draft,
                build_patch=_shipment_patch,
            )
        except Exception:
            # One unresolvable shipment must not stop the rest: a cancelled
            # parcel or a transient ShipX error is expected here, and the next
            # cycle will try again.
            logger.exception("InPost pending resolution failed for draft %s", draft_id)
            stats["errors"] += 1
            continue

        if patch.get("status") != "created":
            stats["still_pending"] += 1
            continue

        patch["shipment_origin"] = SHIPMENT_ORIGIN_SYSTEM
        try:
            shipping_store.update_draft(draft_id, patch)
        except Exception:
            logger.exception("Failed to persist resolved tracking for draft %s", draft_id)
            stats["errors"] += 1
            continue

        _emit_tracking_assigned(draft_id, draft.get("shopify_order_number"), SHIPMENT_ORIGIN_SYSTEM)
        stats["resolved"] += 1

    return stats
