"""zdrovena.api.routers.webhooks — Shopify webhooks + shipping drafts + label endpoints.

POST /webhooks/shopify/order-create          — Shopify order webhook (HMAC-validated)
POST /webhooks/shopify/order-created         — legacy alias for order-create (compat)
GET  /shipping/drafts                         — list shipping drafts from Table Storage
GET  /shipping/drafts/{id}/label              — stream label PDF from courier
POST /shipping/drafts/{id}/execute            — (re)create courier shipment for a draft
POST /shipping/drafts/{id}/pickup             — order InPost kurier pickup
PATCH /shipping/drafts/{id}                   — update packages_count
DELETE /shipping/drafts/{id}/shipment         — cancel Ship-with-Allegro shipment
DELETE /shipping/drafts/{id}/dispatch         — cancel Ship-with-Allegro dispatch
DELETE /inpost/shipments/{id}                 — cancel InPost shipment before dispatch
DELETE /inpost/dispatch_orders/{id}           — cancel InPost dispatch order
DELETE /apaczka/orders/{id}                   — cancel Apaczka order
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import logging
import os
import re
import unicodedata
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Body,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from zdrovena.api import shipping_draft_composition as draft_composition
from zdrovena.api import shipping_execution_composition as execution_composition
from zdrovena.api.auth import Principal, require_shipment_mgr_or_above, require_viewer_or_above
from zdrovena.api.deps import ShippingStoreDep, ShopifyDedupStoreDep, StorageDep
from zdrovena.api.observability import get_correlation_id
from zdrovena.common.appenv import is_production_env
from zdrovena.common.events import log_event
from zdrovena.common.exceptions import MissingSecretError
from zdrovena.common.secrets import get_secret
from zdrovena.common.shipping_exceptions import (
    AllegroAuthError,
    AllegroBusinessError,
    ApaczkaBusinessError,
    CourierAuthError,
    CourierBusinessError,
    CourierTransientError,
    InPostBusinessError,
    LabelNotReadyError,
    ZdrovenaShippingError,
)
from zdrovena.common.shipping_store import (
    DLQ_KIND_CREATION,
    DLQ_KIND_EXECUTION,
    ShippingStore,
)
from zdrovena.common.shopify_dedup_store import DedupStoreError
from zdrovena.shipping.application import drafts as draft_application
from zdrovena.shipping.application.execution import workflow as execution_workflow

logger = logging.getLogger("zdrovena.api.routers.webhooks")

router = APIRouter(tags=["shipping"])


class PickupOrderedResponse(BaseModel):
    status: Literal["pickup_ordered"]
    draft_id: str


class PickupPendingResponse(BaseModel):
    status: Literal["pickup_pending"]
    draft_id: str
    allegro_command_id: str


# ── HMAC helpers ──────────────────────────────────────────────────────────────


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
    return get_secret("shopify_webhook_secret", required=False)


# Topics we actually process. A HMAC-valid payload from any other topic (e.g. a
# mis-configured products/create subscription) would crash _create_draft, so we
# reject unknown topics as defense-in-depth after HMAC.
# NOTE: only `orders/create` is accepted today. `orders/updated` was previously
# whitelisted, but the current handler creates a shipping draft — firing that on
# every order update would produce unwanted duplicate drafts. Once we have a
# dedicated update-handler with clear semantics we can re-add `orders/updated`.
ALLOWED_SHOPIFY_TOPICS = frozenset({"orders/create"})


def _allowed_shopify_domains() -> frozenset[str] | None:
    """Whitelisted shop domains from SHOPIFY_ALLOWED_DOMAINS (comma-separated).

    Returns None when unset — dev mode, all domains accepted (with a warning).
    """
    raw = os.getenv("SHOPIFY_ALLOWED_DOMAINS", "").strip()
    if not raw:
        return None
    return frozenset(d.strip().lower() for d in raw.split(",") if d.strip())


def _is_shopify_topic_allowed(topic: str) -> bool:
    return topic in ALLOWED_SHOPIFY_TOPICS


def _is_production_env() -> bool:
    """True when the canonical ``APP_ENV`` signals a production deploy.

    Delegates to :func:`zdrovena.common.appenv.is_production_env` so the whole
    application resolves "is this production?" from one canonical place (R4-B).
    """
    return is_production_env()


def _test_support_enabled() -> bool:
    return os.getenv("PROVIDER_MODE", "").strip().lower() == "fake" and not _is_production_env()


def _require_test_support() -> None:
    if not _test_support_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


def _is_e2e_record(record: dict[str, Any]) -> bool:
    record_id = str(record.get("id") or "")
    order_number = str(
        record.get("shopify_order_number")
        or record.get("order_number")
        or (record.get("payload") or {}).get("order_number")
        or ""
    )
    return record_id.startswith("e2e-") or order_number.startswith("990")


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
    allowed = _allowed_shopify_domains()
    if allowed is None:
        if _is_production_env():
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


def _get_fakturownia_client() -> Any | None:
    """Build a FakturowniaClient from Key Vault secrets. Returns None if missing."""
    from zdrovena.common.config import KEYCHAIN_SERVICE_FAKTUROWNIA

    try:
        token = get_secret(KEYCHAIN_SERVICE_FAKTUROWNIA)
    except MissingSecretError:
        return None
    from zdrovena.common.client import FakturowniaClient

    return FakturowniaClient(api_token=token)


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

    # High limit: list_drafts fetches all Table Storage rows anyway; the cap
    # only affects the returned slice. 10_000 covers any realistic store size
    # and prevents silent duplicate-draft creation on stores with >200 total orders.
    existing_drafts = shipping_store.list_drafts(limit=10_000)
    existing_by_order_id = {
        str(d.get("external_order_id", "")): d
        for d in existing_drafts
        if d.get("source") == "shopify"
        and d.get("external_order_id")
        and not d.get("is_replacement")
    }

    for order in orders:
        order_id = str(order.get("id", ""))
        try:
            existing = existing_by_order_id.get(order_id)
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


_SHOPIFY_COURIER_COMPANY: dict[str, str] = {
    "inpost": "InPost",
    "apaczka": "Apaczka",
    "allegro_delivery": "Allegro Delivery",
    "allegro": "Allegro Delivery",
}

_SHOPIFY_COURIER_TRACKING_URL: dict[str, str] = {
    "inpost": "https://inpost.pl/sledzenie-przesylek?number={number}",
}


def _sync_shopify_fulfillment(
    order_id: str,
    tracking_number: str | None,
    courier: str | None,
) -> dict[str, Any]:
    """Create a Shopify fulfillment for a completed order via the FulfillmentOrder API.

    Non-blocking: caller decides whether to surface failures as warnings or errors.
    Returns a result dict with "created", "skipped", or "error" key.
    """
    import requests

    shopify_token = get_secret("shopify_admin_token", required=False)
    if not shopify_token:
        return {"skipped": "shopify_not_configured"}

    allowed_domains = _allowed_shopify_domains()
    if not allowed_domains:
        return {"skipped": "no_shopify_domain"}

    shop_domain = next(iter(allowed_domains))
    headers = {
        "X-Shopify-Access-Token": shopify_token,
        "Content-Type": "application/json",
    }
    base = f"https://{shop_domain}/admin/api/2024-01"

    # Step 1: find open fulfillment orders (the modern Shopify fulfillment model)
    fo_resp = requests.get(
        f"{base}/orders/{order_id}/fulfillment_orders.json",
        headers=headers,
        timeout=15,
    )
    fo_resp.raise_for_status()
    open_fo_ids = [
        fo["id"]
        for fo in fo_resp.json().get("fulfillment_orders", [])
        if fo.get("status") == "open"
    ]
    if not open_fo_ids:
        return {"skipped": "no_open_fulfillment_orders"}

    # Step 2: create fulfillment with tracking info
    courier_key = (courier or "").lower()
    tracking_company = _SHOPIFY_COURIER_COMPANY.get(courier_key, courier or "")
    tracking_url_tpl = _SHOPIFY_COURIER_TRACKING_URL.get(courier_key)
    tracking_url = (
        tracking_url_tpl.format(number=tracking_number)
        if tracking_url_tpl and tracking_number
        else None
    )

    payload: dict[str, Any] = {
        "fulfillment": {
            "line_items_by_fulfillment_order": [
                {"fulfillment_order_id": fo_id} for fo_id in open_fo_ids
            ],
            "notify_customer": True,
        }
    }
    if tracking_number:
        tracking_info: dict[str, Any] = {"number": tracking_number, "company": tracking_company}
        if tracking_url:
            tracking_info["url"] = tracking_url
        payload["fulfillment"]["tracking_info"] = tracking_info

    f_resp = requests.post(f"{base}/fulfillments.json", headers=headers, json=payload, timeout=15)
    f_resp.raise_for_status()
    fulfillment = f_resp.json().get("fulfillment", {})
    return {
        "created": True,
        "shopify_fulfillment_id": str(fulfillment.get("id", "")),
        "tracking_number": tracking_number,
    }


_MATCH_MANUAL = "manual"


# ── Webhook endpoint ──────────────────────────────────────────────────────────


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


# ── Drafts list ───────────────────────────────────────────────────────────────


@router.get(
    "/shipping/drafts",
    summary="List shipping drafts",
    responses={403: {"description": "Insufficient role"}},
)
def list_drafts(
    shipping_store: ShippingStoreDep,
    principal: Annotated[Principal, Depends(require_viewer_or_above)],
) -> dict[str, Any]:
    drafts = shipping_store.list_drafts()
    return {"drafts": drafts}


@router.get(
    "/shipping/apaczka-services",
    summary="List the curated Apaczka courier services available for draft selection",
    responses={403: {"description": "Insufficient role"}},
)
def list_apaczka_services(
    principal: Annotated[Principal, Depends(require_viewer_or_above)],
) -> dict[str, Any]:
    from zdrovena.common.apaczka import APACZKA_SERVICE_CATALOG

    return {
        "services": [
            {"service_id": service_id, "label": label}
            for service_id, label in APACZKA_SERVICE_CATALOG.items()
        ]
    }


# ── Dead-letter queue (P1-9) ────────────────────────────────────────────────


@router.get(
    "/shipping/drafts/dlq",
    summary="List failed draft-creation attempts (DLQ)",
    responses={403: {"description": "Insufficient role"}},
)
def list_dlq(
    shipping_store: ShippingStoreDep,
    principal: Annotated[Principal, Depends(require_viewer_or_above)],
) -> dict[str, Any]:
    return {"entries": shipping_store.list_dlq()}


@router.post(
    "/shipping/drafts/dlq/{entry_id}/retry",
    summary="Retry a failed draft-creation attempt from DLQ",
    responses={
        403: {"description": "Insufficient role"},
        404: {"description": "DLQ entry not found"},
        502: {"description": "Retry failed — entry left in DLQ with updated error"},
    },
)
def retry_dlq_entry(
    entry_id: str,
    shipping_store: ShippingStoreDep,
    storage: StorageDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
) -> dict[str, Any]:
    entry = shipping_store.get_dlq_entry(entry_id)
    if not entry:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="DLQ entry not found")
    payload = entry.get("payload") or {}
    source = entry.get("source") or "shopify"
    # Entries written before `kind` existed were all creations.
    kind = entry.get("kind") or DLQ_KIND_CREATION
    try:
        if kind == DLQ_KIND_EXECUTION:
            # The draft already exists — re-run the courier call, never the
            # ingestion, which would duplicate it. Same role guards both
            # endpoints, so reusing the principal grants nothing extra.
            target_draft_id = entry.get("draft_id")
            if not target_draft_id:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="DLQ entry has kind=draft_execution but no draft_id",
                )
            try:
                execution_composition.execute_shipping_draft(
                    target_draft_id,
                    shipping_store,
                    storage,
                    failure_dlq_entry_id=entry_id,
                )
            except execution_composition.EXECUTION_APPLICATION_HTTP_ERRORS as exc:
                execution_composition.raise_execution_http_exception(exc)
        else:
            draft_composition.create_draft(payload, shipping_store, source=source)
    except HTTPException:
        raise
    except ZdrovenaShippingError as exc:
        # Execution retries update their original DLQ entry inside
        # the application workflow before the domain exception is re-raised. Do not
        # increment the same entry a second time here.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Retry failed: {type(exc).__name__}: {exc}",
        ) from exc
    except Exception as exc:
        logger.exception("DLQ retry failed for entry %s: %s", entry_id, exc)
        # bump retries + last_error; keep the entry in DLQ
        try:
            shipping_store.enqueue_dlq(
                payload=payload,
                error=f"{type(exc).__name__}: {exc}",
                source=source,
                entry_id=entry_id,
            )
        except Exception:
            logger.exception("DLQ update after retry failure failed for %s", entry_id)
        # DLQ retry to endpoint diagnostyczny operatora — surowy błąd upstream
        # jest tu celowo zwracany, żeby operator mógł zdecydować o dalszej akcji.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Retry failed: {type(exc).__name__}: {exc}",
        ) from exc
    # success → remove from DLQ
    shipping_store.delete_dlq_entry(entry_id)
    return {"status": "retried", "entry_id": entry_id}


@router.delete(
    "/shipping/drafts/dlq/{entry_id}",
    summary="Discard a DLQ entry without retrying",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        403: {"description": "Insufficient role"},
        404: {"description": "DLQ entry not found"},
    },
)
def delete_dlq_entry(
    entry_id: str,
    shipping_store: ShippingStoreDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
) -> Response:
    entry = shipping_store.get_dlq_entry(entry_id)
    if not entry:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="DLQ entry not found")
    shipping_store.delete_dlq_entry(entry_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── Fake-provider E2E support ────────────────────────────────────────────────


@router.post(
    "/__test__/shipping/reset",
    include_in_schema=False,
    responses={404: {"description": "Disabled outside fake non-production mode"}},
)
def reset_e2e_shipping_state(
    shipping_store: ShippingStoreDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
) -> dict[str, int]:
    _require_test_support()
    removed_drafts = 0
    for draft in shipping_store.list_drafts(limit=200):
        if _is_e2e_record(draft):
            shipping_store.delete_draft(str(draft["id"]))
            removed_drafts += 1

    removed_dlq = 0
    for entry in shipping_store.list_dlq(limit=200):
        if _is_e2e_record(entry):
            shipping_store.delete_dlq_entry(str(entry["id"]))
            removed_dlq += 1

    return {"removed_drafts": removed_drafts, "removed_dlq": removed_dlq}


@router.post(
    "/__test__/shipping/drafts",
    include_in_schema=False,
    responses={404: {"description": "Disabled outside fake non-production mode"}},
)
def seed_e2e_shipping_draft(
    draft: Annotated[dict[str, Any], Body()],
    shipping_store: ShippingStoreDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
) -> dict[str, Any]:
    _require_test_support()
    if not _is_e2e_record(draft):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="E2E draft id must start with e2e- or order number with 990",
        )
    if not draft.get("id"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Draft id required")
    shipping_store.upsert_draft(draft)
    return shipping_store.get_draft(str(draft["id"])) or draft


@router.post(
    "/__test__/shipping/dlq",
    include_in_schema=False,
    responses={404: {"description": "Disabled outside fake non-production mode"}},
)
def seed_e2e_dlq_entry(
    body: Annotated[dict[str, Any], Body()],
    shipping_store: ShippingStoreDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
) -> dict[str, Any]:
    _require_test_support()
    payload = body.get("payload") or {}
    entry_id = str(body.get("id") or "")
    probe = {"id": entry_id, "payload": payload}
    if not _is_e2e_record(probe):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="E2E DLQ id must start with e2e- or payload order number with 990",
        )
    entry = shipping_store.enqueue_dlq(
        payload=payload,
        error=str(body.get("error") or "E2E seeded failure"),
        source=str(body.get("source") or "shopify"),
        entry_id=entry_id or None,
    )
    log_event(
        "dlq.enqueued",
        level=logging.ERROR,
        entry_id=entry["id"],
        order_number=payload.get("order_number") or payload.get("id"),
        source=entry["source"],
        error_type="E2ESeededFailure",
        test_probe=True,
    )
    return entry


# ── Execute draft ─────────────────────────────────────────────────────────────


@router.get(
    "/shipping/drafts/{draft_id}/execute/preview",
    summary="Show exactly what would be sent to the courier, without sending it",
    responses={
        403: {"description": "Insufficient role"},
        404: {"description": "Draft not found"},
    },
)
def preview_execute_draft(
    draft_id: str,
    shipping_store: ShippingStoreDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
    pickup_date: str | None = Query(None),
    pickup_from: str | None = Query(None),
    pickup_to: str | None = Query(None),
) -> dict[str, Any]:
    draft = shipping_store.get_draft(draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")
    try:
        return execution_composition.execution_preview(
            draft,
            pickup_date=pickup_date,
            pickup_from=pickup_from,
            pickup_to=pickup_to,
        )
    except ApaczkaBusinessError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post(
    "/shipping/drafts/{draft_id}/execute",
    summary="(Re)create courier shipment for a draft",
    responses={
        403: {"description": "Insufficient role"},
        404: {"description": "Draft not found"},
        409: {"description": "Draft already executed"},
    },
)
def execute_draft(
    draft_id: str,
    shipping_store: ShippingStoreDep,
    storage: StorageDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
    pickup_date: str | None = Body(None),
    pickup_from: str | None = Body(None),
    pickup_to: str | None = Body(None),
    preview_fingerprint: str | None = Body(None),
) -> dict[str, Any]:
    try:
        return execution_composition.execute_shipping_draft(
            draft_id,
            shipping_store,
            storage,
            pickup_window=execution_workflow.PickupWindow(
                date=pickup_date,
                from_time=pickup_from,
                to_time=pickup_to,
            ),
            preview_fingerprint=preview_fingerprint,
        )
    except execution_composition.EXECUTION_APPLICATION_HTTP_ERRORS as exc:
        execution_composition.raise_execution_http_exception(exc)
    except ApaczkaBusinessError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# ── Confirm pending Allegro create-command ───────────────────────────────────


@router.post(
    "/shipping/drafts/{draft_id}/confirm",
    summary="Poll Allegro create-command and finalise a pending_confirmation draft",
    responses={
        403: {"description": "Insufficient role"},
        404: {"description": "Draft not found"},
        409: {"description": "Draft not in pending_confirmation state"},
        202: {"description": "Still pending"},
        502: {"description": "Allegro API error"},
    },
)
def confirm_pending_command(
    draft_id: str,
    shipping_store: ShippingStoreDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
) -> Any:
    """Poll an outstanding Allegro create-command and finalise the draft.

    Ship-with-Allegro create-commands are asynchronous. ``execute_draft`` returns
    ``pending_confirmation`` when the command is still IN_PROGRESS after the
    short in-request polling window. This endpoint is the durable follow-up:
    call it (via UI action or a cron/worker) to check the command status and
    either promote the draft to ``created`` (SUCCESS) or ``error`` (ERROR).

    Idempotent: safe to call multiple times. Returns the current draft.
    """
    try:
        result = execution_composition.confirm_shipping_draft(draft_id, shipping_store)
    except execution_composition.ConfirmationError as exc:
        execution_composition.raise_confirmation_http_exception(exc)
    if result.status_code == 202:
        return JSONResponse(status_code=202, content=result.payload)
    return result.payload


# ── Pickup (InPost kurier only) ───────────────────────────────────────────────


@router.post(
    "/shipping/drafts/{draft_id}/pickup",
    summary="Order InPost kurier pickup for an executed draft",
    response_model=PickupOrderedResponse,
    responses={
        202: {"model": PickupPendingResponse, "description": "Allegro pickup still pending"},
        403: {"description": "Insufficient role"},
        404: {"description": "Draft not found"},
        409: {"description": "Pickup already ordered or draft not ready"},
        400: {"description": "Courier does not support pickup (not InPost kurier)"},
    },
)
def order_pickup(
    draft_id: str,
    shipping_store: ShippingStoreDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
    pickup_date: str | None = Body(None),
    pickup_from: str | None = Body(None),
    pickup_to: str | None = Body(None),
) -> dict[str, str] | JSONResponse:
    draft = shipping_store.get_draft(draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")
    courier = draft.get("courier")
    # Apaczka is absent on purpose: its API (service_structure / orders /
    # order_send) has no standalone pickup call. An Apaczka pickup can only be
    # requested inside the order_send payload, i.e. at execute time.
    if courier not in {"inpost", "allegro_delivery"}:
        raise HTTPException(
            status_code=400,
            detail="Pickup is only available for InPost and Ship-with-Allegro shipments",
        )
    if draft.get("status") != "created":
        raise HTTPException(status_code=409, detail="Draft must be in 'created' state")
    if draft.get("pickup_ordered"):
        raise HTTPException(status_code=409, detail="Pickup already ordered")

    shipment_ids = [
        str(shipment.get("id") or "").strip() for shipment in draft.get("courier_shipments") or []
    ]
    shipment_ids = [shipment_id for shipment_id in shipment_ids if shipment_id]
    if not shipment_ids:
        legacy_shipment_id = str(
            draft.get("allegro_shipment_id") or draft.get("courier_draft_id") or ""
        ).strip()
        shipment_ids = [legacy_shipment_id] if legacy_shipment_id else []
    if not shipment_ids:
        raise HTTPException(status_code=409, detail="No courier draft ID — execute first")

    # Claim before calling the courier (not after) so two concurrent requests
    # can't both pass the pickup_ordered check above and both dispatch.
    if not shipping_store.try_claim_pickup(draft_id):
        raise HTTPException(status_code=409, detail="Pickup already ordered")

    if execution_composition.MOCK_COURIER:
        ref = draft.get("shopify_order_number", "mock")
        logger.info("MOCK_COURIER: skipping %s pickup for draft %s", courier, ref)
    elif courier == "allegro_delivery":
        existing_command_id = str(draft.get("allegro_pickup_command_id") or "") or None

        def persist_pickup_command(command_id: str) -> None:
            shipping_store.update_draft(
                draft_id,
                {
                    "allegro_pickup_command_id": command_id,
                    "allegro_dispatch_id": None,
                    "pickup_ordered": False,
                },
            )

        try:
            allegro = execution_composition.get_allegro_client()
            if allegro is None:
                raise HTTPException(status_code=502, detail="Allegro credentials missing")
            pickup_result = execution_composition.order_allegro_pickup(
                allegro,
                shipment_ids,
                pickup_date,
                command_id=existing_command_id,
                on_command_created=persist_pickup_command,
            )
            if pickup_result["status"] == "NO_SLOT":
                raise HTTPException(
                    status_code=409,
                    detail="Allegro has no pickup slot available for this shipment",
                )
            if pickup_result["status"] == "IN_PROGRESS":
                shipping_store.update_draft(
                    draft_id,
                    {
                        "pickup_ordered": False,
                        "allegro_pickup_command_id": pickup_result["command_id"],
                        "allegro_dispatch_id": None,
                    },
                )
                return JSONResponse(
                    status_code=202,
                    content={
                        "status": "pickup_pending",
                        "draft_id": draft_id,
                        "allegro_command_id": pickup_result["command_id"],
                    },
                )
            shipping_store.update_draft(
                draft_id,
                {
                    "pickup_ordered": True,
                    "allegro_dispatch_id": pickup_result["pickup_id"],
                    "allegro_pickup_command_id": None,
                },
            )
        except HTTPException:
            shipping_store.update_draft(draft_id, {"pickup_ordered": False})
            raise
        except Exception as exc:
            logger.exception("order_pickup failed for draft %s", draft_id)
            patch: dict[str, Any] = {"pickup_ordered": False}
            if isinstance(exc, execution_composition.AllegroPickupTerminalError):
                patch["allegro_pickup_command_id"] = None
            shipping_store.update_draft(draft_id, patch)
            raise HTTPException(status_code=502, detail=f"Allegro pickup error: {exc}") from exc
    else:
        try:
            dispatch = execution_composition.order_inpost_pickup(
                draft,
                pickup_date,
                pickup_from,
                pickup_to,
            )
        except Exception as exc:
            logger.exception("order_pickup failed for draft %s", draft_id)
            shipping_store.update_draft(draft_id, {"pickup_ordered": False})
            raise HTTPException(status_code=502, detail=f"InPost dispatch error: {exc}") from exc
        # Recorded after the rollback boundary above: the dispatch already exists
        # at this point, so a storage hiccup must not release the pickup claim
        # and invite a duplicate collection. Without the id there is nothing to
        # DELETE, so the pickup could never be cancelled.
        shipping_store.update_draft(
            draft_id, {"dispatch_order_id": str(dispatch.get("id") or "") or None}
        )

    return {"status": "pickup_ordered", "draft_id": draft_id}


# ── Cancel (Ship with Allegro) ────────────────────────────────────────────────


@router.delete(
    "/shipping/drafts/{draft_id}/shipment",
    summary="Cancel a Ship-with-Allegro shipment before dispatch",
    responses={
        403: {"description": "Insufficient role"},
        404: {"description": "Draft not found"},
        409: {"description": "No Allegro shipment to cancel"},
        502: {"description": "Allegro API error"},
    },
)
def cancel_shipment(
    draft_id: str,
    shipping_store: ShippingStoreDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
) -> dict[str, Any]:
    """Cancel the Allegro shipment created for this draft (before it is dispatched)."""
    draft = shipping_store.get_draft(draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")

    shipment_ids = [
        str(shipment.get("id") or "").strip() for shipment in draft.get("courier_shipments") or []
    ]
    shipment_ids = [shipment_id for shipment_id in shipment_ids if shipment_id]
    if not shipment_ids:
        legacy_shipment_id = str(
            draft.get("allegro_shipment_id") or draft.get("courier_draft_id") or ""
        ).strip()
        shipment_ids = [legacy_shipment_id] if legacy_shipment_id else []
    if not shipment_ids:
        raise HTTPException(status_code=409, detail="No Allegro shipment to cancel")

    if not execution_composition.MOCK_COURIER:
        client = execution_composition.get_allegro_client()
        if client is None:
            raise HTTPException(status_code=502, detail="Allegro credentials missing")
        try:
            for shipment_id in shipment_ids:
                client.cancel_ship_with_allegro_shipment(
                    command_id=str(uuid.uuid4()), shipment_id=shipment_id
                )
        except (AllegroBusinessError, AllegroAuthError, CourierTransientError) as exc:
            logger.exception("Allegro cancel shipment failed for draft %s", draft_id)
            raise HTTPException(status_code=502, detail=f"Allegro API error: {exc}") from exc

    shipping_store.update_draft(draft_id, {"status": "cancelled", "allegro_shipment_id": None})
    return {"status": "cancelled", "draft_id": draft_id, "shipment_id": shipment_ids[0]}


@router.delete(
    "/shipping/drafts/{draft_id}/dispatch",
    summary="Cancel a Ship-with-Allegro dispatch (pickup) before acceptance",
    responses={
        403: {"description": "Insufficient role"},
        404: {"description": "Draft not found"},
        409: {"description": "No Allegro dispatch to cancel"},
        502: {"description": "Allegro API error"},
    },
)
def cancel_dispatch(
    draft_id: str,
    shipping_store: ShippingStoreDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
) -> dict[str, Any]:
    """Cancel the Allegro dispatch (pickup) order created for this draft."""
    draft = shipping_store.get_draft(draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")

    dispatch_id = draft.get("allegro_dispatch_id")
    if not dispatch_id:
        raise HTTPException(status_code=409, detail="No Allegro dispatch to cancel")

    if not execution_composition.MOCK_COURIER:
        client = execution_composition.get_allegro_client()
        if client is None:
            raise HTTPException(status_code=502, detail="Allegro credentials missing")
        try:
            client.cancel_ship_with_allegro_dispatch(
                command_id=str(uuid.uuid4()), dispatch_id=str(dispatch_id)
            )
        except (AllegroBusinessError, AllegroAuthError, CourierTransientError) as exc:
            logger.exception("Allegro cancel dispatch failed for draft %s", draft_id)
            raise HTTPException(status_code=502, detail=f"Allegro API error: {exc}") from exc

    shipping_store.update_draft(draft_id, {"pickup_ordered": False, "allegro_dispatch_id": None})
    return {"status": "dispatch_cancelled", "draft_id": draft_id, "dispatch_id": str(dispatch_id)}


# ── Cancel (raw courier id: InPost / Apaczka) ─────────────────────────────────


# Manual fulfillment marking (generic; Allegro side-effect kept for allegro drafts)


@router.post(
    "/shipping/drafts/{draft_id}/mark-fulfilled",
    summary="Manually mark the draft as fulfilled (operator action)",
    responses={
        403: {"description": "Insufficient role"},
        404: {"description": "Draft not found"},
        409: {"description": "Allegro draft has no external Allegro order id"},
        502: {"description": "Allegro API error (only for Allegro drafts)"},
    },
)
def mark_fulfilled(
    draft_id: str,
    shipping_store: ShippingStoreDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
) -> dict[str, Any]:
    """Idempotent operator action to mark the draft as fulfilled.

    A draft only represents "we intend to ship", not "we shipped". The operator
    confirms via the UI once the parcel actually leaves — this endpoint sets the
    local ``fulfillment_status="fulfilled"`` flag (with ``fulfilled_at`` /
    ``fulfilled_by``) for every draft, regardless of source.

    For Allegro drafts we additionally invoke
    ``AllegroClient.mark_order_processed(external_order_id, status="SENT")`` to
    move the order to ``SENT`` on Allegro's side (the parcel has left), and mirror
    the timestamps into the legacy ``allegro_fulfillment_status`` /
    ``allegro_marked_processed_*`` fields.

    Re-running this endpoint is safe: if the draft is already fulfilled we
    return 200 without hitting Allegro again.
    """
    draft = shipping_store.get_draft(draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")

    # R5-A: a cancelled or errored draft was never successfully shipped, so it
    # must not be marked fulfilled (that would push a bogus SENT to Allegro).
    # Re-running on an already-fulfilled draft stays idempotent (handled below).
    if (
        draft.get("status") in ("cancelled", "error")
        and draft.get("fulfillment_status") != "fulfilled"
    ):
        raise HTTPException(
            status_code=409,
            detail="Nie można oznaczyć jako zrealizowane: przesyłka jest anulowana lub w błędzie.",
        )

    is_allegro = draft.get("source") == "allegro"
    external_order_id = (
        draft.get("external_order_id") or draft.get("allegro_order_id") if is_allegro else None
    )
    if is_allegro and not external_order_id:
        raise HTTPException(status_code=409, detail="Allegro draft has no external order id")

    # Idempotency - a second click is a no-op that reports the existing state.
    if draft.get("fulfillment_status") == "fulfilled":
        return {
            "status": "already_fulfilled",
            "draft_id": draft_id,
            "source": draft.get("source"),
            "external_order_id": external_order_id,
            "fulfilled_at": draft.get("fulfilled_at"),
            "fulfilled_by": draft.get("fulfilled_by"),
            "allegro_side_effect": False,
            "shopify_side_effect": None,
        }

    allegro_side_effect = False
    if is_allegro and not execution_composition.MOCK_COURIER:
        client = execution_composition.get_allegro_client()
        if client is None:
            raise HTTPException(status_code=502, detail="Allegro credentials missing")
        try:
            client.mark_order_processed(str(external_order_id), status="SENT")
            allegro_side_effect = True
        except (AllegroBusinessError, AllegroAuthError, CourierTransientError) as exc:
            logger.exception("Allegro mark_order_processed failed for draft %s", draft_id)
            raise HTTPException(status_code=502, detail=f"Allegro API error: {exc}") from exc
    elif is_allegro and execution_composition.MOCK_COURIER:
        # In mock mode we still record that the Allegro side-effect "happened".
        allegro_side_effect = True

    marked_at = datetime.now(timezone.utc).isoformat()
    marked_by = principal.email or principal.sub

    patch: dict[str, Any] = {
        "fulfillment_status": "fulfilled",
        "fulfilled_at": marked_at,
        "fulfilled_by": marked_by,
    }
    if is_allegro:
        # Keep the Allegro-specific mirror fields for backwards compatibility
        # with any UI/report that already reads them.
        patch["allegro_fulfillment_status"] = "SENT"
        patch["allegro_marked_processed_at"] = marked_at
        patch["allegro_marked_processed_by"] = marked_by

    shipping_store.update_draft(draft_id, patch)

    shopify_side_effect: dict[str, Any] | None = None
    is_shopify = draft.get("source") == "shopify"
    if is_shopify:
        shopify_order_id = str(
            draft.get("external_order_id") or draft.get("shopify_order_id") or ""
        )
        if shopify_order_id:
            try:
                shopify_side_effect = _sync_shopify_fulfillment(
                    order_id=shopify_order_id,
                    tracking_number=draft.get("tracking_number"),
                    courier=draft.get("courier"),
                )
            except Exception as exc:
                logger.exception("Shopify fulfillment sync failed for draft %s", draft_id)
                shopify_side_effect = {"error": str(exc)}

    return {
        "status": "marked_fulfilled",
        "draft_id": draft_id,
        "source": draft.get("source"),
        "external_order_id": external_order_id,
        "fulfilled_at": marked_at,
        "fulfilled_by": marked_by,
        "allegro_side_effect": allegro_side_effect,
        "shopify_side_effect": shopify_side_effect,
    }


def _courier_cancel_http_status(exc: ZdrovenaShippingError) -> int:
    """Map a shipping-hierarchy error onto an HTTP status for cancel endpoints.

    Auth -> 401, business (e.g. already dispatched / not cancellable) -> 409,
    transient (network/5xx) -> 503, anything else in the hierarchy -> 500.
    """
    if isinstance(exc, CourierAuthError):
        return status.HTTP_401_UNAUTHORIZED
    if isinstance(exc, CourierBusinessError):
        return status.HTTP_409_CONFLICT
    if isinstance(exc, CourierTransientError):
        return status.HTTP_503_SERVICE_UNAVAILABLE
    return status.HTTP_500_INTERNAL_SERVER_ERROR


def _build_inpost_client() -> Any:
    from zdrovena.common.inpost import InPostClient

    token = get_secret("inpost_api_token")
    org_id = get_secret("inpost_organization_id")
    return InPostClient(token, org_id)


@router.delete(
    "/inpost/shipments/{shipment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Cancel an InPost shipment before dispatch",
    responses={
        403: {"description": "Insufficient role"},
        409: {"description": "Shipment cannot be cancelled (already dispatched / unknown)"},
        503: {"description": "InPost API transient error"},
    },
)
def cancel_inpost_shipment(
    shipment_id: str,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
) -> Response:
    if execution_composition.MOCK_COURIER:
        logger.info("MOCK_COURIER: skipping InPost cancel_shipment for %s", shipment_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    try:
        _build_inpost_client().cancel_shipment(shipment_id)
    except ZdrovenaShippingError as exc:
        logger.exception("InPost cancel_shipment failed for %s", shipment_id)
        raise HTTPException(
            status_code=_courier_cancel_http_status(exc), detail=f"InPost cancel error: {exc}"
        ) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/inpost/dispatch_orders/{dispatch_order_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Cancel an InPost dispatch order before courier acceptance",
    responses={
        403: {"description": "Insufficient role"},
        409: {"description": "Dispatch cannot be cancelled (already accepted / unknown)"},
        503: {"description": "InPost API transient error"},
    },
)
def cancel_inpost_dispatch(
    dispatch_order_id: str,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
) -> Response:
    if execution_composition.MOCK_COURIER:
        logger.info("MOCK_COURIER: skipping InPost cancel_dispatch_order for %s", dispatch_order_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    try:
        _build_inpost_client().cancel_dispatch_order(dispatch_order_id)
    except ZdrovenaShippingError as exc:
        logger.exception("InPost cancel_dispatch_order failed for %s", dispatch_order_id)
        raise HTTPException(
            status_code=_courier_cancel_http_status(exc), detail=f"InPost cancel error: {exc}"
        ) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/apaczka/orders/{order_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Cancel an Apaczka order",
    responses={
        403: {"description": "Insufficient role"},
        409: {"description": "Order cannot be cancelled (already sent / unknown)"},
        503: {"description": "Apaczka API transient error"},
    },
)
def cancel_apaczka_order(
    order_id: str,
    storage: StorageDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
) -> Response:
    if execution_composition.MOCK_COURIER:
        logger.info("MOCK_COURIER: skipping Apaczka cancel for %s", order_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    from zdrovena.common.apaczka import ApaczkaClient

    app_id = get_secret("apaczka_app_id")
    app_secret = get_secret("apaczka_app_secret")
    # No draft available here (only order_id) and cancel_shipment() never
    # reads service_id — pass an empty placeholder rather than looking one up.
    client = ApaczkaClient(app_id, app_secret, "", storage)
    try:
        client.cancel_shipment(order_id)
    except ZdrovenaShippingError as exc:
        logger.exception("Apaczka cancel failed for order %s", order_id)
        raise HTTPException(
            status_code=_courier_cancel_http_status(exc), detail=f"Apaczka cancel error: {exc}"
        ) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── Update packages_count ─────────────────────────────────────────────────────


@router.patch(
    "/shipping/drafts/{draft_id}",
    summary="Update draft metadata (packages_count, service, locker_id)",
    responses={
        403: {"description": "Insufficient role"},
        404: {"description": "Draft not found"},
        400: {"description": "Invalid service for courier"},
    },
)
def update_draft(
    draft_id: str,
    shipping_store: ShippingStoreDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
    packages_count: int | None = Body(None, ge=1, le=99),
    service: str | None = Body(None),
    locker_id: str | None = Body(None),
    apaczka_service_id: str | None = Body(None),
    reviewed: bool | None = Body(None),
) -> dict[str, Any]:
    draft = shipping_store.get_draft(draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")

    patch: dict[str, Any] = {}
    if packages_count is not None:
        patch["packages_count"] = packages_count
    if service is not None:
        valid = {"inpost_locker_standard", "inpost_courier_standard", "apaczka"}
        if service not in valid:
            raise HTTPException(status_code=400, detail=f"Unknown service: {service}")
        if draft.get("courier") == "inpost" and service == "apaczka":
            raise HTTPException(status_code=400, detail="Cannot switch InPost draft to apaczka")
        patch["service"] = service
        patch["shipping_service_match_status"] = _MATCH_MANUAL
        patch["shipping_service_match_source"] = "operator"
        patch["shipping_service_match_detail"] = "Manual service override"
    if locker_id is not None:
        receiver = dict(draft.get("receiver") or {})
        receiver["locker_id"] = locker_id
        patch["receiver"] = receiver
    if apaczka_service_id is not None:
        from zdrovena.common.apaczka import APACZKA_SERVICE_CATALOG

        if apaczka_service_id not in APACZKA_SERVICE_CATALOG:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown apaczka_service_id: {apaczka_service_id}",
            )
        patch["apaczka_service_id"] = apaczka_service_id
        patch["shipping_service_match_status"] = _MATCH_MANUAL
        patch["shipping_service_match_source"] = "operator"
        patch["shipping_service_match_detail"] = "Manual Apaczka service override"
    if reviewed is True and draft.get("status") == "needs_review":
        patch["status"] = "pending"
        patch["error"] = None

    if patch:
        shipping_store.update_draft(draft_id, patch)
    updated = shipping_store.get_draft(draft_id)
    return updated or {"draft_id": draft_id}


# ── Label streaming ───────────────────────────────────────────────────────────

_SUPPORTED_LABEL_COURIERS = ("inpost", "apaczka", "allegro_delivery")
_MAX_BATCH_LABELS = 100  # provider-agnostic safety cap on one batch print


def _safe_label_filename(courier: str, order_number: Any) -> str:
    """Return an ASCII-only filename safe for a quoted response header."""
    normalized = unicodedata.normalize("NFKD", str(order_number).lstrip("#"))
    ascii_order = normalized.encode("ascii", "ignore").decode("ascii")
    safe_order = re.sub(r"[^A-Za-z0-9._-]+", "_", ascii_order).strip("._-")
    safe_order = safe_order[:80] or "order"
    return f"label_{courier}_{safe_order}.pdf"


def _fetch_label_pdf(draft: dict[str, Any], courier: str, storage: Any) -> bytes:
    """Fetch one label PDF for a draft. Shared by the single-label and batch
    endpoints (R5-B).

    Raises :class:`LabelNotReadyError` (HTTP 409) when the label is not printable
    yet — either the draft has no courier id, or InPost rejects the fetch with a
    business error (almost always "shipment not confirmed/processed yet"). Other
    courier failures surface as HTTP 502.
    """
    if courier == "allegro_delivery":
        label_ids = execution_composition.dispatch_shipment_ids(draft)
    else:
        label_ids = [shipment.get("id") for shipment in draft.get("courier_shipments") or []]
        if not label_ids:
            label_ids = [draft.get("courier_draft_id")]
    label_ids = [str(label_id) for label_id in label_ids if label_id]
    if not label_ids:
        raise HTTPException(status_code=404, detail="No courier draft ID — draft may have failed")

    try:
        if courier == "inpost":
            from zdrovena.common.inpost import InPostClient

            token = get_secret("inpost_api_token")
            org_id = get_secret("inpost_organization_id")
            try:
                pdfs = [InPostClient(token, org_id).get_label(label_id) for label_id in label_ids]
                return _merge_pdfs(pdfs) if len(pdfs) > 1 else pdfs[0]
            except InPostBusinessError as exc:
                # A business rejection while fetching a label means the shipment
                # is not confirmed/processed yet → not ready, not a hard failure.
                raise LabelNotReadyError(str(exc), courier="inpost", action="get_label") from exc
        elif courier == "apaczka":
            from zdrovena.common.apaczka import ApaczkaClient

            app_id = get_secret("apaczka_app_id")
            app_secret = get_secret("apaczka_app_secret")
            service_id = draft.get("apaczka_service_id") or ""
            client = ApaczkaClient(app_id, app_secret, service_id, storage)
            pdfs = [client.get_label(label_id) for label_id in label_ids]
            return _merge_pdfs(pdfs) if len(pdfs) > 1 else pdfs[0]
        else:  # allegro_delivery
            client = execution_composition.get_allegro_client()
            if client is None:
                raise HTTPException(status_code=502, detail="Allegro credentials missing")
            try:
                pdfs = [client.get_ship_with_allegro_label(label_id) for label_id in label_ids]
                return _merge_pdfs(pdfs) if len(pdfs) > 1 else pdfs[0]
            except (AllegroBusinessError, AllegroAuthError, CourierTransientError) as exc:
                logger.exception("Allegro label fetch failed for draft %s", draft.get("id"))
                raise HTTPException(status_code=502, detail=f"Allegro API error: {exc}") from exc
    except (HTTPException, ZdrovenaShippingError):
        raise
    except Exception as exc:
        logger.exception("Label fetch failed for draft %s", draft.get("id"))
        raise HTTPException(status_code=502, detail=f"Courier API error: {exc}") from exc


def _merge_pdfs(pdfs: list[bytes]) -> bytes:
    """Merge label PDFs into a single document (R5-B batch printing)."""
    from pypdf import PdfWriter

    writer = PdfWriter()
    for pdf in pdfs:
        writer.append(io.BytesIO(pdf))
    out = io.BytesIO()
    writer.write(out)
    writer.close()
    return out.getvalue()


@router.post(
    "/shipping/labels/batch",
    summary="Fetch and merge labels for several drafts into one printable PDF",
    responses={
        400: {"description": "No draft_ids, too many, or unsupported courier"},
        404: {"description": "None of the drafts exist"},
        409: {"description": "One or more labels are not ready yet"},
    },
)
def batch_labels(
    shipping_store: ShippingStoreDep,
    storage: StorageDep,
    principal: Annotated[Principal, Depends(require_viewer_or_above)],
    draft_ids: Annotated[list[str], Body(embed=True)],
) -> StreamingResponse:
    """Merge the labels of the given drafts into one PDF (R5-B).

    Drafts are grouped by courier (each fetched via the same path as the single
    label endpoint), then concatenated in the request order. Fails deterministically:
      * empty / oversized ``draft_ids`` → 400
      * a not-yet-ready label → 409 listing the offending drafts
      * an unknown draft id → 404 listing them
    """
    if not draft_ids:
        raise HTTPException(status_code=400, detail="draft_ids must not be empty")
    if len(draft_ids) > _MAX_BATCH_LABELS:
        raise HTTPException(
            status_code=400,
            detail=f"Too many labels in one batch (max {_MAX_BATCH_LABELS}, got {len(draft_ids)})",
        )

    drafts: list[dict[str, Any]] = []
    missing: list[str] = []
    for did in draft_ids:
        d = shipping_store.get_draft(did)
        if d is None:
            missing.append(did)
        else:
            drafts.append(d)
    if missing:
        raise HTTPException(status_code=404, detail=f"Draft(s) not found: {', '.join(missing)}")

    bad_courier = [d.get("id") for d in drafts if d.get("courier") not in _SUPPORTED_LABEL_COURIERS]
    if bad_courier:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported courier for draft(s): {', '.join(map(str, bad_courier))}",
        )

    # Group by courier so a future provider bulk-label API can be slotted in per
    # group; today we fetch each label and merge. Order within the response
    # follows the original draft_ids order for predictable printing.
    pdfs: list[bytes] = []
    not_ready: list[str] = []
    for d in drafts:
        try:
            pdfs.append(_fetch_label_pdf(d, d["courier"], storage))
        except LabelNotReadyError:
            not_ready.append(str(d.get("id")))
        except HTTPException as exc:
            # A missing courier id (404) means the draft exists but has no label
            # yet — for a batch that is just another "not ready" case, not a hard
            # failure. Any other courier error (502) aborts the whole batch.
            if exc.status_code == 404:
                not_ready.append(str(d.get("id")))
            else:
                raise
    if not_ready:
        raise HTTPException(
            status_code=409,
            detail=f"Etykiety nie są jeszcze gotowe dla: {', '.join(not_ready)}",
        )

    merged = _merge_pdfs(pdfs)
    return StreamingResponse(
        io.BytesIO(merged),
        media_type="application/pdf",
        headers={"Content-Disposition": 'inline; filename="labels_batch.pdf"'},
    )


@router.get(
    "/shipping/drafts/{draft_id}/label",
    summary="Stream shipping label PDF",
    responses={403: {"description": "Insufficient role"}, 404: {"description": "Draft not found"}},
)
def get_label(
    draft_id: str,
    shipping_store: ShippingStoreDep,
    storage: StorageDep,
    principal: Annotated[Principal, Depends(require_viewer_or_above)],
    courier: str = Query(
        None, description="inpost, apaczka, or allegro_delivery (defaults to draft's courier)"
    ),
) -> StreamingResponse:
    draft = shipping_store.get_draft(draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")

    # Prefer the stored draft courier over the query param (prevents mismatch)
    courier = draft.get("courier") or courier
    _SUPPORTED_COURIERS = ("inpost", "apaczka", "allegro_delivery")
    if courier not in _SUPPORTED_COURIERS:
        raise HTTPException(
            status_code=400,
            detail=f"courier must be one of: {', '.join(_SUPPORTED_COURIERS)}",
        )

    pdf_bytes = _fetch_label_pdf(draft, courier, storage)

    filename = _safe_label_filename(courier, draft.get("shopify_order_number", draft_id))
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


# ── Allegro invoice (manual) ──────────────────────────────────────────────────


def _get_fakturownia_invoice_client() -> Any | None:
    """Build zdrovena.common.fakturownia.FakturowniaClient for invoice CRUD.

    Distinct from _get_fakturownia_client() which returns the audit-only
    common.client.FakturowniaClient (paginated date-range fetch only).
    """
    from zdrovena.common.config import DEFAULT_DOMAIN, KEYCHAIN_SERVICE_FAKTUROWNIA

    try:
        token = get_secret(KEYCHAIN_SERVICE_FAKTUROWNIA)
    except MissingSecretError:
        return None
    from zdrovena.common.fakturownia import FakturowniaClient

    base_url = os.getenv("FAKTUROWNIA_BASE_URL", "").strip() or f"https://{DEFAULT_DOMAIN}"
    return FakturowniaClient(api_token=token, base_url=base_url)


@router.get(
    "/shipping/drafts/{draft_id}/invoice-preview",
    summary="Compute Fakturownia invoice preview for an Allegro order",
    responses={
        400: {"description": "Not an Allegro draft"},
        404: {"description": "Draft not found"},
        503: {"description": "Allegro credentials not configured"},
    },
)
def get_invoice_preview(
    draft_id: str,
    shipping_store: ShippingStoreDep,
    principal: Annotated[Principal, Depends(require_viewer_or_above)],
) -> dict[str, Any]:
    from decimal import Decimal

    draft = shipping_store.get_draft(draft_id)
    if not draft:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Draft not found")
    if draft.get("source") != "allegro":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invoice preview only for Allegro orders",
        )

    existing = draft.get("fakturownia_invoice_id")
    invoice_error = draft.get("fakturownia_invoice_error")
    if existing and not invoice_error:
        return {"status": "already_created", "fakturownia_invoice_id": existing}
    if existing and invoice_error:
        return {
            "status": "retry_ready",
            "fakturownia_invoice_id": existing,
            "error": invoice_error,
        }

    allegro_client = execution_composition.get_allegro_client()
    if allegro_client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Allegro credentials not configured",
        )

    order_id = draft.get("external_order_id") or draft.get("shopify_order_number", "")
    order = allegro_client.get_order(order_id)

    from zdrovena.common.allegro_invoice_mapper import (
        allegro_expected_payable,
        allegro_order_to_fakturownia_invoice,
    )

    payload = allegro_order_to_fakturownia_invoice(order)
    positions = payload.get("positions") or []
    settlements = payload.get("settlement_positions") or []

    positions_total = sum(Decimal(str(p.get("total_price_gross", 0))) for p in positions)
    settlement_total = sum(Decimal(str(s.get("amount", 0))) for s in settlements)
    total = positions_total + settlement_total

    buyer = order.get("buyer") or {}
    invoice_req = order.get("invoice") or {}
    addr = invoice_req.get("address") or buyer.get("address") or {}
    company = addr.get("company") or {}

    # Cross-check "Do zapłaty" (positions + kaucja) against Allegro's own
    # summary.totalToPay minus delivery (invoice has no shipping line), via the
    # shared allegro_expected_payable helper so preview and final invoice compare
    # against the identical figure. `difference` is the signed, explainable delta
    # (our total − Allegro's) so a mismatch is inspectable, not just a boolean.
    allegro_expected = allegro_expected_payable(order)
    allegro_total_to_pay: float | None = None
    matches_allegro: bool | None = None
    difference: float | None = None
    if allegro_expected is not None:
        allegro_total_to_pay = float(allegro_expected)
        delta = total - allegro_expected
        difference = float(delta)
        matches_allegro = abs(delta) <= Decimal("0.01")

    return {
        "status": "preview_ready",
        "buyer_name": payload.get(
            "buyer_name", f"{buyer.get('firstName', '')} {buyer.get('lastName', '')}".strip()
        ),
        "buyer_email": payload.get("buyer_email", buyer.get("email", "")),
        "buyer_company": company.get("name") or None,
        "buyer_nip": company.get("taxId") or None,
        "positions": [
            {
                "name": p["name"],
                "quantity": p["quantity"],
                "unit_price_gross": float(Decimal(str(p["total_price_gross"])) / p["quantity"])
                if p.get("quantity")
                else 0.0,
                "vat_rate": f"{int(p.get('tax', 0))}%",
                "line_total": float(p["total_price_gross"]),
            }
            for p in positions
        ],
        "settlement_positions": [
            {"description": s.get("description", ""), "amount": float(s.get("amount", 0) or 0)}
            for s in settlements
        ],
        "positions_total": float(positions_total),
        "settlement_total": float(settlement_total),
        "total_gross": float(total),
        "allegro_total_to_pay": allegro_total_to_pay,
        "matches_allegro": matches_allegro,
        "difference": difference,
    }


@router.post(
    "/shipping/drafts/{draft_id}/create-invoice",
    summary="Create Fakturownia invoice for an Allegro order and attach it",
    responses={
        400: {"description": "Not an Allegro draft"},
        404: {"description": "Draft not found"},
        503: {"description": "Credentials not configured"},
    },
)
def create_draft_invoice(
    draft_id: str,
    shipping_store: ShippingStoreDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
) -> dict[str, Any]:
    draft = shipping_store.get_draft(draft_id)
    if not draft:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Draft not found")
    if draft.get("source") != "allegro":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invoice creation only for Allegro orders",
        )

    existing = draft.get("fakturownia_invoice_id")
    invoice_error = draft.get("fakturownia_invoice_error")
    if existing and existing != "pending" and not invoice_error:
        return {"status": "already_created", "fakturownia_invoice_id": existing}
    if existing == "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Invoice creation already in progress — try again in a moment",
        )

    # Claim the slot optimistically so concurrent requests see "pending" and bail out.
    shipping_store.update_draft(
        draft_id,
        {"fakturownia_invoice_id": "pending", "fakturownia_invoice_error": None},
    )

    allegro_client = execution_composition.get_allegro_client()
    fakturownia_client = _get_fakturownia_invoice_client()
    if allegro_client is None:
        shipping_store.update_draft(
            draft_id,
            {
                "fakturownia_invoice_id": existing,
                "fakturownia_invoice_error": invoice_error,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Allegro credentials not configured",
        )
    if fakturownia_client is None:
        shipping_store.update_draft(
            draft_id,
            {
                "fakturownia_invoice_id": existing,
                "fakturownia_invoice_error": invoice_error,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Fakturownia credentials not configured",
        )

    order_id = draft.get("external_order_id") or draft.get("shopify_order_number", "")
    try:
        order = allegro_client.get_order(order_id)
    except Exception as exc:
        shipping_store.update_draft(
            draft_id,
            {"fakturownia_invoice_id": existing, "fakturownia_invoice_error": str(exc)},
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to fetch Allegro order: {exc}",
        ) from exc

    from zdrovena.api.routers.allegro_invoicer import create_invoice_for_order

    result = create_invoice_for_order(
        order, fakturownia_client=fakturownia_client, allegro_client=allegro_client
    )

    result_status = result.get("status")

    # "already_exists" is a success: Fakturownia already holds the invoice for
    # this order (idempotent create via oid). Persist the recovered id and
    # report "already_created" — never 502, never reset state to None (that was
    # the loop bug: clearing the slot re-armed the poller to try forever).
    if result_status == "already_exists":
        recovered_id = result.get("fakturownia_invoice_id")
        shipping_store.update_draft(
            draft_id,
            {
                "fakturownia_invoice_id": recovered_id,
                "fakturownia_invoice_number": result.get("fakturownia_invoice_number"),
                "fakturownia_invoice_error": None,
            },
        )
        return {
            "status": "already_created",
            "fakturownia_invoice_id": recovered_id,
            "fakturownia_invoice_number": result.get("fakturownia_invoice_number"),
        }

    if result_status != "created":
        # On failure, keep any invoice id Fakturownia already produced (e.g. the
        # invoice was created but the Allegro push failed) so a retry attaches to
        # the same document instead of orphaning it. Only clear the slot when we
        # truly have nothing to keep.
        recovered_id = result.get("fakturownia_invoice_id") or existing
        shipping_store.update_draft(
            draft_id,
            {
                "fakturownia_invoice_id": recovered_id,
                "fakturownia_invoice_number": result.get("fakturownia_invoice_number")
                or draft.get("fakturownia_invoice_number"),
                "fakturownia_invoice_error": result.get("error", "Invoice creation failed"),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=result.get("error", "Invoice creation failed"),
        )
    shipping_store.update_draft(
        draft_id,
        {
            "fakturownia_invoice_id": result["fakturownia_invoice_id"],
            "fakturownia_invoice_number": result.get("fakturownia_invoice_number"),
            "fakturownia_invoice_error": None,
        },
    )
    return result


@router.post(
    "/shipping/sync",
    status_code=status.HTTP_200_OK,
    summary="Manually trigger order sync from Allegro and Shopify",
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

    shopify_token = get_secret("shopify_admin_token", required=False)
    allowed_domains = _allowed_shopify_domains()
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
