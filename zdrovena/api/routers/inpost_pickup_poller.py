"""zdrovena.api.routers.inpost_pickup_poller — pick up InPost pickups ordered elsewhere.

``pickup_ordered`` / ``dispatch_order_id`` used to be set only when the portal
itself ordered the courier (``routers/shipping/fulfillment.py``). An operator who
booked the pickup in the InPost app or InPost Manager left the draft looking
un-picked-up, and the portal kept offering "Zamów podjazd" — a second, duplicate
pickup for parcels the courier was already coming for (#552, from #543).

This module reads the organisation's ShipX dispatch orders and marks every
created InPost draft whose shipment is in a live one. It runs from the same
scheduled cycle as the tracking resolver (``inpost_poller.py``) and follows the
structure of ``apaczka_pickup_poller.py``. The operator's "Sprawdź status" path
calls :func:`sync_inpost_pickup_for_draft` so the result shows without waiting.

Read only: nothing here creates or cancels a dispatch order.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from zdrovena.common.secrets import get_secret

logger = logging.getLogger("zdrovena.api.routers.inpost_pickup_poller")

# ShipX dispatch order statuses after which no courier is coming. "rejected" is
# InPost refusing the pickup — as dead as an operator cancellation.
_INACTIVE_DISPATCH_STATUSES = frozenset({"canceled", "cancelled", "rejected"})


def _mock_courier() -> bool:
    """Read at call time, not import time, so tests and dev can toggle it."""
    return os.environ.get("MOCK_COURIER", "").strip() == "1"


def _draft_shipment_ids(draft: dict[str, Any]) -> set[str]:
    ids = {
        str(shipment.get("id") or "").strip() for shipment in draft.get("courier_shipments") or []
    }
    ids.add(str(draft.get("courier_draft_id") or "").strip())
    ids.discard("")
    return ids


def needs_pickup_sync(draft: dict[str, Any]) -> bool:
    """A created InPost draft with a ShipX shipment and no pickup recorded."""
    return (
        draft.get("courier") == "inpost"
        and draft.get("status") == "created"
        and not draft.get("pickup_ordered")
        and bool(_draft_shipment_ids(draft))
    )


def _dispatch_index(dispatch_orders: list[dict[str, Any]]) -> dict[str, str]:
    """Map shipment id -> id of the live dispatch order that collects it."""
    index: dict[str, str] = {}
    for order in dispatch_orders:
        status = str(order.get("status") or "").strip().lower()
        if status in _INACTIVE_DISPATCH_STATUSES:
            continue
        order_id = str(order.get("id") or "").strip()
        if not order_id:
            continue
        for shipment in order.get("shipments") or []:
            raw = shipment.get("id") if isinstance(shipment, dict) else shipment
            shipment_id = str(raw or "").strip()
            if shipment_id:
                # First live order wins; a second live order for the same
                # parcel is InPost's duplicate, not something to choose between.
                index.setdefault(shipment_id, order_id)
    return index


def _pickup_patch(draft: dict[str, Any], index: dict[str, str]) -> dict[str, Any] | None:
    for shipment_id in sorted(_draft_shipment_ids(draft)):
        dispatch_order_id = index.get(shipment_id)
        if dispatch_order_id:
            return {"pickup_ordered": True, "dispatch_order_id": dispatch_order_id}
    return None


def _default_client() -> Any:
    from zdrovena.common.inpost import InPostClient

    return InPostClient(
        get_secret("inpost_api_token"),
        get_secret("inpost_organization_id"),
    )


def sync_inpost_pickups_once(
    *,
    shipping_store: Any,
    client: Any = None,
) -> dict[str, int]:
    """One cycle: mark drafts whose shipment is in a live ShipX dispatch order.

    Returns per-cycle stats. Never raises: this runs inside a scheduled job that
    must survive a bad draft or a courier outage. An InPost error leaves every
    draft as it was; the next cycle tries again.
    """
    stats = {"scanned": 0, "marked": 0, "unmatched": 0, "errors": 0}

    if _mock_courier():
        logger.info("MOCK_COURIER: skipping InPost pickup sync")
        return stats

    try:
        drafts = shipping_store.list_drafts(limit=10_000)
    except Exception:
        # Resilience boundary: a store read failure must not abort the cycle.
        logger.exception("shipping_store.list_drafts failed")
        stats["errors"] += 1
        return stats

    candidates = [draft for draft in drafts if needs_pickup_sync(draft)]
    stats["scanned"] = len(candidates)
    if not candidates:
        return stats

    try:
        if client is None:
            client = _default_client()
        index = _dispatch_index(client.list_dispatch_orders())
    except Exception:
        logger.exception("InPost dispatch order lookup failed — pickup sync skipped")
        stats["errors"] += 1
        return stats

    for draft in candidates:
        draft_id = str(draft.get("id") or "")
        patch = _pickup_patch(draft, index)
        if patch is None:
            stats["unmatched"] += 1
            continue
        try:
            shipping_store.update_draft(draft_id, patch)
        except Exception:
            logger.exception("Failed to persist external InPost pickup for draft %s", draft_id)
            stats["errors"] += 1
            continue
        logger.info(
            "InPost pickup ordered outside the portal: draft=%s dispatch_order=%s",
            draft_id,
            patch["dispatch_order_id"],
        )
        stats["marked"] += 1

    return stats


def sync_inpost_pickup_for_draft(
    draft: dict[str, Any],
    *,
    shipping_store: Any,
    client: Any = None,
) -> dict[str, Any] | None:
    """Run the same check for one draft (the operator's "Sprawdź status").

    Returns the persisted patch, or ``None`` when nothing changed. Never raises:
    the caller has already done its real work, and a failed lookup here only
    means the scheduled cycle will catch the pickup instead.
    """
    if _mock_courier() or not needs_pickup_sync(draft):
        return None
    draft_id = str(draft.get("id") or "")
    try:
        if client is None:
            client = _default_client()
        patch = _pickup_patch(draft, _dispatch_index(client.list_dispatch_orders()))
        if patch is None:
            return None
        shipping_store.update_draft(draft_id, patch)
    except Exception:
        logger.exception("InPost pickup sync failed for draft %s", draft_id)
        return None
    logger.info(
        "InPost pickup ordered outside the portal: draft=%s dispatch_order=%s",
        draft_id,
        patch["dispatch_order_id"],
    )
    return patch


__all__ = ["needs_pickup_sync", "sync_inpost_pickup_for_draft", "sync_inpost_pickups_once"]
