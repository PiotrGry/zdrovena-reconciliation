"""zdrovena.api.routers.allegro_poller — poll Allegro for new orders.

Allegro has no webhooks, so we periodically poll ``GET /order/checkout-forms``
for orders in status ``READY_FOR_PROCESSING``. Each new order is mapped to a
Shopify-like payload and pushed through the existing ``_create_draft`` pipeline
so shipping logic (package calc, courier picking, phone/address normalisation)
is reused as-is.

Idempotency: a draft is created only if there is no existing non-error draft
with the same ``(source='allegro', external_order_id=<allegro id>)`` pair.

Errors on one order do not block the others.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from zdrovena.api.routers.webhooks import _create_draft
from zdrovena.common.allegro_mapper import allegro_to_shopify_order

logger = logging.getLogger("zdrovena.api.routers.allegro_poller")


def _existing_active_allegro_draft(
    drafts: list[dict[str, Any]], external_order_id: str
) -> dict[str, Any] | None:
    for d in drafts:
        if (
            d.get("source") == "allegro"
            and str(d.get("external_order_id", "")) == str(external_order_id)
            and d.get("status") != "error"
        ):
            return d
    return None


def poll_orders_once(
    *,
    client: Any,
    shipping_store: Any,
    storage: Any,
    status: str = "READY_FOR_PROCESSING",
) -> dict[str, int]:
    """One polling cycle. Returns per-cycle stats."""
    stats = {
        "fetched": 0,
        "created": 0,
        "skipped_duplicate": 0,
        "errors": 0,
    }
    try:
        forms = client.list_orders(status=status)
    except Exception as exc:
        logger.error("Allegro list_orders failed: %s", exc)
        stats["errors"] += 1
        return stats

    stats["fetched"] = len(forms)
    if not forms:
        return stats

    try:
        drafts = shipping_store.list_drafts()
    except Exception as exc:
        logger.error("shipping_store.list_drafts failed: %s", exc)
        stats["errors"] += 1
        drafts = []

    for form in forms:
        allegro_id = str(form.get("id", ""))
        if not allegro_id:
            logger.warning("Allegro checkout-form without id — skipping")
            stats["errors"] += 1
            continue

        if _existing_active_allegro_draft(drafts, allegro_id):
            logger.info("Allegro order %s already has a draft — skipping", allegro_id)
            stats["skipped_duplicate"] += 1
            continue

        try:
            shopify_like = allegro_to_shopify_order(form)
            _create_draft(shopify_like, shipping_store, storage, source="allegro")
        except Exception as exc:
            logger.error("Failed to create draft for Allegro order %s: %s", allegro_id, exc)
            stats["errors"] += 1
            continue

        stats["created"] += 1

        # Bezpieczny default: NIE oznaczamy zamówienia jako PROCESSING po samym utworzeniu draftu —
        # sam draft nie oznacza jeszcze nadania. Docelowo oznaczenie powinno paść w execute_draft po
        # sukcesie create-command. Za flagą ALLEGRO_MARK_ON_DRAFT=1 zachowujemy stare zachowanie.
        if os.getenv("ALLEGRO_MARK_ON_DRAFT", "").strip() in ("1", "true", "True"):
            try:
                client.mark_order_processed(allegro_id)
            except Exception as exc:
                logger.warning(
                    "Draft created but mark_order_processed failed for %s: %s",
                    allegro_id,
                    exc,
                )

    return stats
