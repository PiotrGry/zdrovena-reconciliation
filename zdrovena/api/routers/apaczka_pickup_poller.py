"""zdrovena.api.routers.apaczka_pickup_poller — fill in missing Apaczka pickup ids.

Apaczka's ``order_send`` answers without a pickup block; the carrier assigns the
pickup number afterwards and exposes it on ``order/:id/``. Execution reads it
once, best-effort (see ``_apaczka_pickup_number`` in
``shipping_execution_composition.py``), which leaves the id empty whenever the
carrier had not got around to it yet.

That is the id the operator quotes to Apaczka support when a collection goes
wrong, so "empty because nobody looked again" is not an acceptable resting
state. This module is the second look, run from the same scheduled cycle as the
InPost tracking resolver (``inpost_poller.py``, the model this module follows).
"""

from __future__ import annotations

import logging
import os
from typing import Any

from zdrovena.common.secrets import get_secret

logger = logging.getLogger("zdrovena.api.routers.apaczka_pickup_poller")

# An order the carrier never numbers must not be retried forever. Five cycles
# is roughly a working day at the current schedule.
MAX_PICKUP_NUMBER_ATTEMPTS = 5


def _mock_courier() -> bool:
    """Read at call time, not import time, so tests and dev can toggle it."""
    return os.environ.get("MOCK_COURIER", "").strip() == "1"


def _needs_pickup_number(draft: dict[str, Any]) -> bool:
    if draft.get("courier") != "apaczka" or draft.get("status") != "created":
        return False
    if int(draft.get("pickup_number_attempts") or 0) >= MAX_PICKUP_NUMBER_ATTEMPTS:
        return False
    return any(
        str(shipment.get("id") or "").strip()
        and not str(shipment.get("pickup_number") or "").strip()
        for shipment in draft.get("courier_shipments") or []
    )


def resolve_apaczka_pickup_numbers_once(
    *,
    shipping_store: Any,
    client: Any = None,
    storage: Any = None,
) -> dict[str, int]:
    """One resolution cycle over Apaczka drafts missing a pickup number.

    Returns per-cycle stats. Never raises: this runs inside a scheduled job that
    must survive a bad draft or a carrier outage.

    ``storage`` is the blob ``StorageService`` used to build the default
    client (it backs Apaczka's own service-structure/points cache) — it is
    unrelated to ``shipping_store``, which only holds shipping drafts. It is
    read only when ``client`` is not supplied; production wiring needs it,
    the unit tests never exercise that branch.
    """
    stats = {"scanned": 0, "resolved": 0, "still_pending": 0, "errors": 0}

    if _mock_courier():
        logger.info("MOCK_COURIER: skipping Apaczka pickup number resolution")
        return stats

    try:
        # High limit for the same reason the InPost resolver uses one: the
        # default page hides the oldest rows, and a draft still missing its
        # pickup id is exactly the kind that has been sitting around.
        drafts = shipping_store.list_drafts(limit=10_000)
    except Exception:
        # Resilience boundary: a store read failure must not abort the cycle.
        logger.exception("shipping_store.list_drafts failed")
        stats["errors"] += 1
        return stats

    pending = [draft for draft in drafts if _needs_pickup_number(draft)]
    stats["scanned"] = len(pending)
    if not pending:
        return stats

    if client is None:
        try:
            from zdrovena.common.apaczka import ApaczkaClient

            # service_id only matters to build_shipment_order/create_shipment;
            # get_order_pickup_number never reads it. One client built once —
            # not per draft, and not keyed to any particular draft's service —
            # covers every pending draft regardless of which Apaczka service
            # it originally shipped through.
            client = ApaczkaClient(
                get_secret("apaczka_app_id"),
                get_secret("apaczka_app_secret"),
                "",
                storage,
            )
        except Exception:
            logger.exception("Apaczka credentials unavailable — cannot resolve pickup numbers")
            stats["errors"] += 1
            return stats

    for draft in pending:
        draft_id = str(draft.get("id") or "")
        shipments = [dict(shipment) for shipment in draft.get("courier_shipments") or []]
        resolved_any = False
        try:
            for shipment in shipments:
                order_id = str(shipment.get("id") or "").strip()
                if not order_id or str(shipment.get("pickup_number") or "").strip():
                    continue
                number = client.get_order_pickup_number(order_id)
                if number:
                    shipment["pickup_number"] = number
                    resolved_any = True
        except Exception:
            # One unresolvable order must not stop the rest: a cancelled parcel
            # or a transient Apaczka error is expected here, and the next cycle
            # will try again.
            logger.exception("Apaczka pickup number resolution failed for draft %s", draft_id)
            stats["errors"] += 1
            continue

        patch = {
            "courier_shipments": shipments,
            "pickup_number_attempts": int(draft.get("pickup_number_attempts") or 0) + 1,
        }
        try:
            shipping_store.update_draft(draft_id, patch)
        except Exception:
            logger.exception("Failed to persist Apaczka pickup numbers for draft %s", draft_id)
            stats["errors"] += 1
            continue

        if resolved_any:
            stats["resolved"] += 1
        else:
            stats["still_pending"] += 1

    return stats


__all__ = ["MAX_PICKUP_NUMBER_ATTEMPTS", "resolve_apaczka_pickup_numbers_once"]
