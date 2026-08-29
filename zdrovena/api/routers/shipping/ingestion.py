"""Shopify webhook ingestion and the operator-triggered sync."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Request,
    status,
)

from zdrovena.api import shipping_draft_composition as draft_composition
from zdrovena.api import shipping_execution_composition as execution_composition
from zdrovena.api.auth import Principal, require_shipment_mgr_or_above
from zdrovena.api.deps import ShippingStoreDep, ShopifyDedupStoreDep, StorageDep
from zdrovena.api.models import (
    ShippingSyncResponse,
)
from zdrovena.api.observability import get_correlation_id
from zdrovena.api.routers.shipping import deps
from zdrovena.common.events import log_event
from zdrovena.common.shipping_store import (
    ShippingStore,
)
from zdrovena.common.shopify_dedup_store import DedupStoreError
from zdrovena.shipping.application import drafts as draft_application

logger = logging.getLogger("zdrovena.api.routers.shipping.ingestion")

router = APIRouter(tags=["shipping"])


def _verify_shopify_hmac(raw_body: bytes, signature_header: str, secret: str) -> bool:
    computed = base64.b64encode(
        hmac.new(secret.encode(), raw_body, hashlib.sha256).digest()
    ).decode()
    if not hmac.compare_digest(computed, signature_header):
        # Log truncated details to speed up local HMAC debugging without
        # leaking the full secret or signature to production logs.
        logger.warning(
            "HMAC mismatch: computed=%s... received=%s... body_len=%d",
            computed[:16],
            signature_header[:16],
            len(raw_body),
        )
        return False
    return True


def _get_webhook_secret() -> str | None:
    return deps.get_secret("shopify_webhook_secret", required=False)


# Topics we actually process. A HMAC-valid payload from any other topic (e.g. a
# mis-configured products/create subscription) would crash _create_draft, so we
# reject unknown topics as defense-in-depth after HMAC.
# NOTE: only `orders/create` is accepted today. `orders/updated` was previously
# whitelisted, but the current handler creates a shipping draft — firing that on
# every order update would produce unwanted duplicate drafts. Once we have a
# dedicated update-handler with clear semantics we can re-add `orders/updated`.
ALLOWED_SHOPIFY_TOPICS = frozenset({"orders/create"})


def _is_shopify_topic_allowed(topic: str) -> bool:
    return topic in ALLOWED_SHOPIFY_TOPICS


def _is_shopify_domain_allowed(shop_domain: str) -> bool:
    """Return True when the shop domain is on the SHOPIFY_ALLOWED_DOMAINS whitelist.

    Fail-closed policy:
      * SHOPIFY_ALLOWED_DOMAINS unset in a **production** environment is a
        misconfiguration — we reject the webhook rather than silently accept
        every caller. Production is detected via APP_ENV/DEPLOY_ENV/AZURE_ENV/ENV
        being one of {production, prod, live}.
      * SHOPIFY_ALLOWED_DOMAINS unset in dev/sandbox/staging keeps the previous
        permissive behaviour (with a warning) so local development doesn't
        require boilerplate config.
    """
    allowed = deps._allowed_shopify_domains()
    if allowed is None:
        if deps._is_production_env():
            logger.error(
                "SHOPIFY_ALLOWED_DOMAINS is not configured in production — "
                "rejecting webhook from %s",
                shop_domain or "<missing>",
            )
            return False
        logger.warning(
            "SHOPIFY_ALLOWED_DOMAINS not configured — accepting webhook from %s (dev mode)",
            shop_domain or "<missing>",
        )
        return True
    return shop_domain.lower() in allowed


def _sync_shopify_orders_from_api(
    shop_domain: str,
    api_token: str,
    shipping_store: ShippingStore,
    storage: Any,
) -> dict[str, int]:
    """Fetch Shopify orders via REST API and create or refresh shipping drafts.

    Uses external_order_id (Shopify order id) for idempotency. Existing drafts
    are refreshed instead of skipped so the visible list reflects status changes
    made outside this app.
    """
    import requests

    stats: dict[str, int] = {
        "fetched": 0,
        "created": 0,
        "updated": 0,
        "unchanged": 0,
        "errors": 0,
    }
    # limit=50 is intentional for v1 — covers typical daily volume.
    # If >50 recently open orders pile up, a second sync call will catch the rest.
    # No cursor pagination implemented.
    resp = requests.get(
        f"https://{shop_domain}/admin/api/2024-01/orders.json",
        params={
            "status": "any",
            "fulfillment_status": "any",
            "order": "updated_at desc",
            "limit": 50,
            "fields": (
                "id,order_number,name,email,phone,created_at,updated_at,"
                "cancelled_at,closed_at,financial_status,fulfillment_status,"
                "fulfillments,shipping_address,shipping_lines,line_items,"
                "note_attributes,customer,gateway,payment_gateway_names,"
                "total_outstanding,currency"
            ),
        },
        headers={"X-Shopify-Access-Token": api_token},
        timeout=15,
    )
    resp.raise_for_status()
    orders = resp.json().get("orders", [])
    stats["fetched"] = len(orders)
    if not orders:
        return stats

    for order in orders:
        order_id = str(order.get("id", ""))
        try:
            # Targeted lookup, not an index built from list_drafts: that index
            # silently omitted every row past its limit, so an order that
            # existed read as new and was written again (#316).
            existing = shipping_store.find_draft_by_external_id(
                source="shopify", external_order_id=order_id
            )
            changed = draft_application.sync_draft_from_order(
                order,
                shipping_store,
                build_draft_record=draft_composition.build_draft_record,
                emit_tracking_assigned=draft_composition.emit_tracking_assigned,
                record_event=log_event,
                send_new_order_sms=draft_composition.maybe_send_new_order_sms,
                source="shopify",
                existing=existing,
            )
            if existing is None:
                stats["created"] += 1
            elif changed:
                stats["updated"] += 1
            else:
                stats["unchanged"] += 1
        except Exception:
            logger.exception(
                "Shopify sync: draft refresh failed for order %s", order.get("order_number")
            )
            stats["errors"] += 1

    return stats


@router.post(
    "/webhooks/shopify/order-create",
    status_code=status.HTTP_200_OK,
    summary="Shopify order webhook — creates shipping draft",
    include_in_schema=False,
)
@router.post(
    # Legacy alias. Some Shopify webhook subscriptions in the shop admin still
    # point at /order-created (Shopify's own topic key is `orders/create`, but
    # the endpoint URL is operator-defined). Both paths execute the same
    # handler so renaming the primary path is never a breaking change.
    "/webhooks/shopify/order-created",
    status_code=status.HTTP_200_OK,
    summary="Shopify order webhook — legacy alias",
    include_in_schema=False,
)
async def shopify_order_created(
    request: Request,
    background_tasks: BackgroundTasks,
    shipping_store: ShippingStoreDep,
    dedup_store: ShopifyDedupStoreDep,
) -> dict[str, str]:
    raw_body = await request.body()

    # .strip() defends against proxies/tools that append trailing whitespace or
    # newlines to the header value (observed with cloudflared tunneled tests).
    sig_header = request.headers.get("X-Shopify-Hmac-Sha256", "").strip()
    webhook_id = request.headers.get("X-Shopify-Webhook-Id", "")
    topic = request.headers.get("X-Shopify-Topic", "")
    shop_domain = request.headers.get("X-Shopify-Shop-Domain", "")
    webhook_secret = _get_webhook_secret()

    # 1. HMAC — always required. There is no unsigned bypass: an unsigned payload
    #    could forge orders and ship parcels to arbitrary addresses.
    if not webhook_secret:
        logger.warning("shopify-webhook-secret not configured — rejecting webhook")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Webhook secret not configured",
        )
    if not sig_header:
        logger.warning("Shopify webhook received without HMAC header — rejected")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing signature")
    if not _verify_shopify_hmac(raw_body, sig_header, webhook_secret):
        logger.warning("Shopify webhook HMAC mismatch — rejected")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature")

    # 2. Whitelist topic + shop domain (defense-in-depth after HMAC).
    if not _is_shopify_topic_allowed(topic):
        logger.warning(
            "Shopify webhook with disallowed topic %r (id=%s) — rejected", topic, webhook_id
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Topic not allowed")
    if not _is_shopify_domain_allowed(shop_domain):
        logger.warning(
            "Shopify webhook from disallowed shop %r (id=%s) — rejected", shop_domain, webhook_id
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Shop domain not allowed")

    # 3. Parse the (now trusted) body.
    try:
        order = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON") from exc

    # 4. Deduplicate by X-Shopify-Webhook-Id atomically. `mark_seen_if_new` does
    #    a single check-and-set (Azure: create_entity+ResourceExistsError, local:
    #    load→check→save under flock) so two concurrent deliveries can never both
    #    proceed. Fail-closed (503) if the dedup store is unavailable so Shopify
    #    retries rather than us risking a duplicate draft.
    if webhook_id:
        try:
            inserted = dedup_store.mark_seen_if_new(webhook_id)
        except DedupStoreError:
            logger.exception("Shopify dedup store unavailable for webhook %s", webhook_id)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Dedup store unavailable",
            ) from None
        if not inserted:
            logger.info("Duplicate Shopify webhook %s — skipping", webhook_id)
            return {"status": "duplicate", "webhook_id": webhook_id}
    else:
        logger.warning("Shopify webhook missing X-Shopify-Webhook-Id — dedup skipped")

    # 5. Orders without shipping lines never become drafts.
    if not order.get("shipping_lines"):
        logger.warning("Order %s has no shipping_lines — skipping draft", order.get("id"))
        return {"status": "skipped"}

    # 6. Heavy work off the request path (Shopify enforces a 5s timeout).
    #    Correlation ID przekazujemy jawnie — kontekst żądania jest już zresetowany,
    #    gdy Starlette wykonuje zadanie tła, więc log draftu inaczej straciłby powiązanie.
    background_tasks.add_task(
        draft_composition.create_draft_safely,
        order,
        shipping_store,
        correlation_id=get_correlation_id(),
    )
    logger.info("Queued shipping draft for order %s", order.get("order_number") or order.get("id"))
    return {"status": "accepted"}


@router.post(
    "/shipping/sync",
    status_code=status.HTTP_200_OK,
    summary="Manually trigger order sync from Allegro and Shopify",
    response_model=ShippingSyncResponse,
    response_model_exclude_unset=True,
)
def sync_orders(
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
    shipping_store: ShippingStoreDep,
    storage: StorageDep,
) -> dict[str, Any]:
    result: dict[str, Any] = {"allegro": None, "shopify": None}

    allegro_client = execution_composition.get_allegro_client()
    if allegro_client is not None:
        try:
            from zdrovena.api.routers.allegro_poller import poll_orders_once

            # This operator action intentionally performs an import/status sync only.
            # Passing a Fakturownia client here would invoice every historical paid
            # order that is new to our local store.
            result["allegro"] = poll_orders_once(
                client=allegro_client,
                shipping_store=shipping_store,
                storage=storage,
                fakturownia_client=None,
                fulfillment_status=None,
                retry_existing_invoices=False,
            )
        except Exception as exc:
            logger.exception("Allegro sync failed: %s", exc)
            result["allegro"] = {"error": str(exc)}
    else:
        result["allegro"] = {"error": "credentials_not_configured"}

    shopify_token = deps.get_secret("shopify_admin_token", required=False)
    allowed_domains = deps._allowed_shopify_domains()
    if shopify_token and allowed_domains:
        shop_domain = next(iter(allowed_domains))
        try:
            result["shopify"] = _sync_shopify_orders_from_api(
                shop_domain=shop_domain,
                api_token=shopify_token,
                shipping_store=shipping_store,
                storage=storage,
            )
        except Exception as exc:
            logger.exception("Shopify sync failed: %s", exc)
            result["shopify"] = {"error": str(exc)}
    else:
        result["shopify"] = {"skipped": "not_configured"}

    log_event(
        "sync.completed",
        actor_id=principal.sub,
        allegro=result["allegro"],
        shopify=result["shopify"],
    )
    return result
