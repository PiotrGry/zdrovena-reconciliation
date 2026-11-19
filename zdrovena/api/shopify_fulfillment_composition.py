"""Shopify fulfillment for shipping drafts: the HTTP, persistence and flag (#541).

Two callers share :func:`sync_draft_with_shopify`:

* the operator's "mark fulfilled" action (always, flag or not);
* :func:`maybe_auto_fulfill`, run when a Shopify draft first gets a tracking
  number — at execution for synchronous carriers, at confirmation, and in the
  InPost poller once a waybill resolves. Gated by ``SHOPIFY_AUTO_FULFILL``
  (default off).

The line split and carrier mapping are in
:mod:`zdrovena.shipping.domain.shopify_fulfillment`. This module owns:

* idempotency — the Shopify fulfillment ids are stored on the draft; once they
  exist without an error, no caller creates another fulfillment;
* failure handling — the error is stored on the draft
  (``shopify_fulfillment_error``), never raised into the label path, and
  :func:`retry_shopify_auto_fulfillments_once` retries it on the next poller
  cycle.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from zdrovena.common.events import log_event
from zdrovena.shipping.domain import shopify_fulfillment as plan_rules

logger = logging.getLogger("zdrovena.api.shopify_fulfillment_composition")

AUTO_FULFILL_ENV = "SHOPIFY_AUTO_FULFILL"
AUTO_FULFILL_ACTOR = "system:shopify_auto_fulfill"
SHOPIFY_API_VERSION = "2024-01"
_HTTP_TIMEOUT_S = 15

# Draft fields this module writes. Also preserved across a Shopify order
# refresh (see ``_SYNC_PRESERVED_FIELDS``).
FIELD_IDS = "shopify_fulfillment_ids"
FIELD_ERROR = "shopify_fulfillment_error"
FIELD_ATTEMPTED_AT = "shopify_fulfillment_attempted_at"
FIELD_SYNCED_AT = "shopify_fulfillment_synced_at"

_TRUTHY = {"1", "true", "yes", "on"}


def auto_fulfill_enabled() -> bool:
    """``SHOPIFY_AUTO_FULFILL`` read at call time, so tests and ops can flip it."""
    return os.getenv(AUTO_FULFILL_ENV, "").strip().lower() in _TRUTHY


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _shopify_order_id(draft: dict[str, Any]) -> str:
    return str(draft.get("external_order_id") or draft.get("shopify_order_id") or "")


def _shopify_api() -> tuple[str, dict[str, str]] | dict[str, str]:
    """``(base_url, headers)`` or a ``{"skipped": reason}`` when not configured."""
    # Lazy: the shipping router package imports the composition modules.
    from zdrovena.api.routers.shipping import deps

    token = deps.get_secret("shopify_admin_token", required=False)
    if not token:
        return {"skipped": "shopify_not_configured"}
    allowed_domains = deps._allowed_shopify_domains()
    if not allowed_domains:
        return {"skipped": "no_shopify_domain"}
    shop_domain = sorted(allowed_domains)[0]
    headers = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}
    return f"https://{shop_domain}/admin/api/{SHOPIFY_API_VERSION}", headers


def _create_fulfillments(
    base: str,
    headers: dict[str, str],
    order_id: str,
    draft: dict[str, Any],
    *,
    on_created: Callable[[str], None],
) -> dict[str, Any]:
    """Create the planned fulfillments in Shopify. Raises on any HTTP failure."""
    import requests

    fo_resp = requests.get(
        f"{base}/orders/{order_id}/fulfillment_orders.json",
        headers=headers,
        timeout=_HTTP_TIMEOUT_S,
    )
    fo_resp.raise_for_status()
    fulfillment_orders = fo_resp.json().get("fulfillment_orders", [])
    if not plan_rules.open_fulfillment_order_lines(fulfillment_orders):
        return {"skipped": "no_open_fulfillment_orders"}

    order_resp = requests.get(
        f"{base}/orders/{order_id}.json",
        params={"fields": "id,line_items"},
        headers=headers,
        timeout=_HTTP_TIMEOUT_S,
    )
    order_resp.raise_for_status()
    order_line_items = (order_resp.json().get("order") or {}).get("line_items")

    plan = plan_rules.plan_fulfillments(fulfillment_orders, order_line_items)
    tracking_number = str(draft.get("tracking_number") or "").strip()
    tracking = plan_rules.tracking_info(draft, tracking_number) if tracking_number else None

    created: list[dict[str, Any]] = []
    for planned in plan:
        f_resp = requests.post(
            f"{base}/fulfillments.json",
            headers=headers,
            json=planned.payload(tracking),
            timeout=_HTTP_TIMEOUT_S,
        )
        f_resp.raise_for_status()
        fulfillment_id = str((f_resp.json().get("fulfillment") or {}).get("id") or "")
        on_created(fulfillment_id)
        created.append(
            {
                "kind": planned.kind,
                "shopify_fulfillment_id": fulfillment_id,
                "notify_customer": planned.notify_customer,
            }
        )
    return {
        "created": True,
        "shopify_fulfillment_id": created[0]["shopify_fulfillment_id"] if created else "",
        "tracking_number": tracking_number or None,
        "tracking_company": (tracking or {}).get("company"),
        "fulfillments": created,
    }


def _persist(repository: Any, draft_id: str, patch: dict[str, Any]) -> None:
    try:
        repository.update_draft(draft_id, patch)
    except Exception:
        # The Shopify side already happened; the next attempt reads Shopify's
        # open lines, so a lost write cannot turn into a second fulfillment.
        logger.exception("Failed to persist Shopify fulfillment state for draft %s", draft_id)


def sync_draft_with_shopify(draft: dict[str, Any], repository: Any) -> dict[str, Any]:
    """Fulfill a Shopify draft's order once. Never raises.

    Returns one of:

    * ``{"reused": True, ...}`` — the draft already holds its fulfillment ids;
    * ``{"created": True, ...}`` — fulfillments created now, ids stored;
    * ``{"skipped": reason}`` — not configured, no order id, or nothing open;
    * ``{"error": message, ...}`` — Shopify failed; stored on the draft.
    """
    draft_id = str(draft.get("id") or "")
    existing_ids = [str(i) for i in draft.get(FIELD_IDS) or [] if i]
    if existing_ids and not draft.get(FIELD_ERROR):
        return {
            "reused": True,
            "shopify_fulfillment_id": existing_ids[0],
            "shopify_fulfillment_ids": existing_ids,
        }

    order_id = _shopify_order_id(draft)
    if not order_id:
        return {"skipped": "no_shopify_order_id"}

    api = _shopify_api()
    if isinstance(api, dict):
        return api
    base, headers = api

    created_ids: list[str] = []
    attempted_at = _now()
    try:
        result = _create_fulfillments(base, headers, order_id, draft, on_created=created_ids.append)
    except Exception as exc:
        logger.exception("Shopify fulfillment sync failed for draft %s", draft_id)
        all_ids = existing_ids + [i for i in created_ids if i]
        error = f"{type(exc).__name__}: {exc}"
        _persist(
            repository,
            draft_id,
            {FIELD_IDS: all_ids, FIELD_ERROR: error, FIELD_ATTEMPTED_AT: attempted_at},
        )
        log_event(
            "shopify.fulfillment_failed",
            level=logging.WARNING,
            draft_id=draft_id,
            order_number=draft.get("shopify_order_number"),
            error=error,
        )
        return {"error": str(exc), "shopify_fulfillment_ids": all_ids}

    if result.get("skipped"):
        if draft.get(FIELD_ERROR):
            # Nothing left open in Shopify: whatever failed earlier is done now
            # (or was done by hand), so stop retrying it.
            _persist(repository, draft_id, {FIELD_ERROR: None, FIELD_ATTEMPTED_AT: attempted_at})
        return result

    all_ids = existing_ids + [i for i in created_ids if i]
    _persist(
        repository,
        draft_id,
        {
            FIELD_IDS: all_ids,
            FIELD_ERROR: None,
            FIELD_ATTEMPTED_AT: attempted_at,
            FIELD_SYNCED_AT: attempted_at,
        },
    )
    log_event(
        "shopify.fulfillment_created",
        draft_id=draft_id,
        order_number=draft.get("shopify_order_number"),
        tracking_number=result.get("tracking_number"),
        tracking_company=result.get("tracking_company"),
        shopify_fulfillment_ids=all_ids,
    )
    return {**result, "shopify_fulfillment_ids": all_ids}


def _auto_fulfill_eligible(draft: dict[str, Any]) -> bool:
    if draft.get("source") != "shopify":
        return False
    if draft.get("status") != "created":
        return False
    if not str(draft.get("tracking_number") or "").strip():
        return False
    if draft.get(FIELD_IDS) and not draft.get(FIELD_ERROR):
        return False
    # Already fulfilled by hand without a pending Shopify error: leave it.
    return not (draft.get("fulfillment_status") == "fulfilled" and not draft.get(FIELD_ERROR))


def maybe_auto_fulfill(draft_id: str, repository: Any) -> dict[str, Any] | None:
    """Fulfill the Shopify order of a draft that has a tracking number. Never raises.

    ``None`` when the flag is off or the draft is not eligible (not Shopify, no
    tracking yet, already synced). Otherwise the :func:`sync_draft_with_shopify`
    result; on success the draft is marked fulfilled like the manual action.
    """
    if not auto_fulfill_enabled():
        return None
    try:
        draft = repository.get_draft(draft_id)
        if not draft or not _auto_fulfill_eligible(draft):
            return None
        result = sync_draft_with_shopify(draft, repository)
        if (result.get("created") or result.get("reused")) and draft.get(
            "fulfillment_status"
        ) != "fulfilled":
            _persist(
                repository,
                draft_id,
                {
                    "fulfillment_status": "fulfilled",
                    "fulfilled_at": _now(),
                    "fulfilled_by": AUTO_FULFILL_ACTOR,
                },
            )
        return result
    except Exception:
        # Resilience boundary: labels and tracking already exist; this is a
        # follow-up the next poller cycle can redo.
        logger.exception("Shopify auto-fulfill failed for draft %s", draft_id)
        return {"error": "auto_fulfill_failed"}


def retry_shopify_auto_fulfillments_once(*, shipping_store: Any) -> dict[str, int]:
    """Retry drafts whose Shopify fulfillment failed earlier. Never raises."""
    stats = {"scanned": 0, "fulfilled": 0, "errors": 0}
    if not auto_fulfill_enabled():
        return stats
    try:
        drafts = shipping_store.list_drafts(limit=10_000)
    except Exception:
        logger.exception("shipping_store.list_drafts failed")
        stats["errors"] += 1
        return stats

    for draft in drafts:
        if not draft.get(FIELD_ERROR) or not _auto_fulfill_eligible(draft):
            continue
        stats["scanned"] += 1
        result = maybe_auto_fulfill(str(draft.get("id") or ""), shipping_store) or {}
        if result.get("created") or result.get("reused"):
            stats["fulfilled"] += 1
        elif result.get("error"):
            stats["errors"] += 1
    return stats


def auto_fulfill_and_refresh(
    draft_id: str, repository: Any, current: dict[str, Any]
) -> dict[str, Any]:
    """Run :func:`maybe_auto_fulfill` and return the draft as it is afterwards.

    For the execution/confirmation responses: when nothing happened the caller's
    own payload is returned unchanged.
    """
    result = maybe_auto_fulfill(draft_id, repository)
    if result is None:
        return current
    try:
        return repository.get_draft(draft_id) or current
    except Exception:
        logger.exception("Failed to re-read draft %s after Shopify auto-fulfill", draft_id)
        return current
