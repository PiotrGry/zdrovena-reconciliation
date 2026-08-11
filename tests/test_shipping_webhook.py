"""Tests for zdrovena.api.routers.webhooks — HMAC validation, courier routing, and endpoints."""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import logging
import os
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

os.environ.setdefault("AZURE_AUTH_DISABLED", "true")

from zdrovena.api.main import app
from zdrovena.api.routers import webhooks as webhooks_router
from zdrovena.api.routers.webhooks import (
    _pick_courier,
    _verify_shopify_hmac,
)
from zdrovena.common.apaczka import ApaczkaClient
from zdrovena.common.inpost import InPostClient
from zdrovena.common.shipping_store import ShippingStore
from zdrovena.common.shopify_dedup_store import ShopifyDedupStore
from zdrovena.shipping.application import drafts as draft_application
from zdrovena.shipping.domain.models import PackageBreakdownItem, PackagePlan, PhysicalParcel
from zdrovena.shipping.domain.planning import (
    calc_packages,
    parcel_weight_and_dims,
    physical_parcels,
    shipment_reference,
)
from zdrovena.shipping.providers.allegro_delivery import (
    allegro_payload_plan as allegro_delivery_payload_plan,
)
from zdrovena.shipping.providers.apaczka import apaczka_payload_plan
from zdrovena.shipping.providers.inpost import inpost_call_specs, inpost_payload_plan

_FIXTURES = Path(__file__).parent / "fixtures"

_WEBHOOK_SECRET = "test-webhook-secret"


def _load_fixture(name: str) -> dict:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def _provider_inpost_payload_plan(
    draft: dict[str, Any], sender: dict[str, str]
) -> list[dict[str, Any]]:
    return inpost_payload_plan(draft, sender, InPostClient("preview", "preview"))


def _provider_apaczka_payload_plan(
    draft: dict[str, Any], pickup_address: dict[str, str]
) -> list[dict[str, Any]]:
    service_id = str(draft.get("apaczka_service_id") or "")
    return apaczka_payload_plan(
        draft,
        pickup_address,
        ApaczkaClient("preview", "preview", service_id, None),
    )


def _draft_application_kwargs() -> dict[str, Any]:
    return {
        "build_draft_record": webhooks_router._build_draft_record,
        "emit_tracking_assigned": webhooks_router._emit_tracking_assigned,
        "record_event": webhooks_router.log_event,
        "send_new_order_sms": webhooks_router._maybe_send_new_order_sms,
    }


def _create_draft_for_test(
    order: dict[str, Any],
    shipping_store: Any,
    storage: Any,
    *,
    source: str = "shopify",
) -> dict[str, Any]:
    del storage
    return draft_application.create_draft(
        order,
        shipping_store,
        source=source,
        **_draft_application_kwargs(),
    )


def _sync_draft_from_order_for_test(
    order: dict[str, Any],
    shipping_store: Any,
    storage: Any,
    *,
    source: str = "shopify",
    existing: dict[str, Any] | None = None,
) -> bool:
    del storage
    return draft_application.sync_draft_from_order(
        order,
        shipping_store,
        source=source,
        existing=existing,
        **_draft_application_kwargs(),
    )


def _merge_synced_draft_for_test(
    existing: dict[str, Any], incoming: dict[str, Any]
) -> dict[str, Any]:
    return draft_application.merge_synced_draft(
        existing,
        incoming,
        emit_tracking_assigned=webhooks_router._emit_tracking_assigned,
    )


# ── Pure helpers ──────────────────────────────────────────────────────────────


def _sign(body: bytes, secret: str) -> str:
    return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


def _shopify_headers(
    body: bytes,
    secret: str = _WEBHOOK_SECRET,
    *,
    topic: str = "orders/create",
    webhook_id: str | None = "wh-test-1",
) -> dict[str, str]:
    """Build valid Shopify webhook headers (HMAC + topic + optional delivery id)."""
    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Hmac-Sha256": _sign(body, secret),
        "X-Shopify-Topic": topic,
    }
    if webhook_id is not None:
        headers["X-Shopify-Webhook-Id"] = webhook_id
    return headers


class TestVerifyShopifyHmac:
    def test_valid_signature(self):
        body = b'{"id":1}'
        sig = _sign(body, "my-secret")
        assert _verify_shopify_hmac(body, sig, "my-secret") is True

    def test_invalid_signature(self):
        body = b'{"id":1}'
        assert _verify_shopify_hmac(body, "not-valid", "my-secret") is False

    def test_wrong_secret(self):
        body = b'{"id":1}'
        sig = _sign(body, "correct-secret")
        assert _verify_shopify_hmac(body, sig, "wrong-secret") is False

    def test_tampered_body(self):
        body = b'{"id":1}'
        sig = _sign(body, "secret")
        assert _verify_shopify_hmac(b'{"id":2}', sig, "secret") is False


class TestPickCourier:
    def test_paczkomat_keyword_routes_to_inpost(self):
        order = {"shipping_lines": [{"title": "InPost Paczkomat 24"}]}
        assert _pick_courier(order) == "inpost"

    def test_kurier_keyword_routes_to_inpost(self):
        order = {"shipping_lines": [{"title": "InPost Kurier ekspresowy"}]}
        assert _pick_courier(order) == "inpost"

    def test_dpd_routes_to_apaczka(self):
        order = {"shipping_lines": [{"title": "Wysyłka DPD"}]}
        assert _pick_courier(order) == "apaczka"

    def test_unknown_title_routes_to_apaczka(self):
        order = {"shipping_lines": [{"title": "Odbiór osobisty"}]}
        assert _pick_courier(order) == "apaczka"

    def test_empty_shipping_lines_defaults_to_apaczka(self):
        assert _pick_courier({"shipping_lines": []}) == "apaczka"

    def test_missing_shipping_lines_defaults_to_apaczka(self):
        assert _pick_courier({}) == "apaczka"

    def test_case_insensitive(self):
        order = {"shipping_lines": [{"title": "INPOST PACZKOMAT"}]}
        assert _pick_courier(order) == "inpost"


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def store(tmp_path) -> ShippingStore:
    return ShippingStore(local_root=tmp_path / "store")


@pytest.fixture()
def dedup_store(tmp_path) -> ShopifyDedupStore:
    return ShopifyDedupStore(local_root=tmp_path / "dedup")


@pytest.fixture()
def client(tmp_path, store, dedup_store):
    from zdrovena.common.storage import LocalStorageService

    storage = LocalStorageService(root=tmp_path / "storage")
    with patch("zdrovena.api.deps._storage_singleton", return_value=storage):
        with patch("zdrovena.api.deps._shipping_store_singleton", return_value=store):
            with patch("zdrovena.api.deps._shopify_dedup_singleton", return_value=dedup_store):
                with TestClient(app, raise_server_exceptions=True) as c:
                    yield c


_ORDER_NO_SHIPPING = json.dumps({"id": 999, "order_number": 1001}).encode()
_ORDER_WITH_SHIPPING = json.dumps(
    {
        "id": 1,
        "order_number": 1042,
        "shipping_lines": [{"title": "DPD Kurier"}],
        "shipping_address": {
            "first_name": "Jan",
            "last_name": "Kowalski",
            "address1": "Kwiatowa 1",
            "city": "Warszawa",
            "zip": "00-001",
        },
        "customer": {"email": "jan@example.com", "phone": "500000000"},
    }
).encode()


# ── Webhook endpoint ──────────────────────────────────────────────────────────


class TestWebhookEndpoint:
    def test_no_shipping_lines_returns_skipped(self, client):
        with patch(
            "zdrovena.api.routers.webhooks._get_webhook_secret", return_value=_WEBHOOK_SECRET
        ):
            resp = client.post(
                "/api/webhooks/shopify/order-create",
                content=_ORDER_NO_SHIPPING,
                headers=_shopify_headers(_ORDER_NO_SHIPPING, webhook_id="wh-skip"),
            )
        assert resp.status_code == 200
        assert resp.json() == {"status": "skipped"}

    def test_no_secret_configured_rejects_with_503(self, client):
        """No configured secret → 503 (no unsigned bypass exists anymore)."""
        with patch("zdrovena.api.routers.webhooks._get_webhook_secret", return_value=None):
            resp = client.post(
                "/api/webhooks/shopify/order-create",
                content=_ORDER_WITH_SHIPPING,
                headers=_shopify_headers(_ORDER_WITH_SHIPPING),
            )
        assert resp.status_code == 503
        assert "not configured" in resp.json()["detail"]

    def test_valid_hmac_accepted(self, client):
        with patch(
            "zdrovena.api.routers.webhooks._get_webhook_secret", return_value=_WEBHOOK_SECRET
        ):
            with patch("zdrovena.shipping.application.drafts.create_draft"):
                resp = client.post(
                    "/api/webhooks/shopify/order-create",
                    content=_ORDER_WITH_SHIPPING,
                    headers=_shopify_headers(_ORDER_WITH_SHIPPING),
                )
        assert resp.status_code == 200
        assert resp.json() == {"status": "accepted"}

    def test_legacy_order_created_alias_accepted(self, client):
        """Legacy alias /order-created must route to the same handler as /order-create.

        Ensures existing Shopify webhook subscriptions pointing at the old URL
        keep working — not a breaking change.
        """
        with patch(
            "zdrovena.api.routers.webhooks._get_webhook_secret", return_value=_WEBHOOK_SECRET
        ):
            with patch("zdrovena.shipping.application.drafts.create_draft"):
                resp = client.post(
                    "/api/webhooks/shopify/order-created",
                    content=_ORDER_WITH_SHIPPING,
                    headers=_shopify_headers(_ORDER_WITH_SHIPPING, webhook_id="wh-legacy-alias"),
                )
        assert resp.status_code == 200
        assert resp.json() == {"status": "accepted"}

    def test_invalid_hmac_rejected(self, client):
        with patch(
            "zdrovena.api.routers.webhooks._get_webhook_secret", return_value=_WEBHOOK_SECRET
        ):
            resp = client.post(
                "/api/webhooks/shopify/order-create",
                content=_ORDER_WITH_SHIPPING,
                headers={
                    "Content-Type": "application/json",
                    "X-Shopify-Hmac-Sha256": "bad",
                    "X-Shopify-Topic": "orders/create",
                },
            )
        assert resp.status_code == 401

    def test_missing_hmac_header_with_secret_configured_rejected(self, client):
        with patch(
            "zdrovena.api.routers.webhooks._get_webhook_secret", return_value=_WEBHOOK_SECRET
        ):
            resp = client.post(
                "/api/webhooks/shopify/order-create",
                content=_ORDER_WITH_SHIPPING,
                headers={"Content-Type": "application/json", "X-Shopify-Topic": "orders/create"},
            )
        assert resp.status_code == 401

    def test_invalid_json_returns_400(self, client):
        bad = b"not-json"
        with patch(
            "zdrovena.api.routers.webhooks._get_webhook_secret", return_value=_WEBHOOK_SECRET
        ):
            resp = client.post(
                "/api/webhooks/shopify/order-create",
                content=bad,
                headers=_shopify_headers(bad, webhook_id="wh-badjson"),
            )
        assert resp.status_code == 400

    def test_disallowed_topic_rejected_403(self, client):
        with patch(
            "zdrovena.api.routers.webhooks._get_webhook_secret", return_value=_WEBHOOK_SECRET
        ):
            resp = client.post(
                "/api/webhooks/shopify/order-create",
                content=_ORDER_WITH_SHIPPING,
                headers=_shopify_headers(_ORDER_WITH_SHIPPING, topic="products/create"),
            )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Topic not allowed"

    def test_missing_topic_rejected_403(self, client):
        headers = {
            "Content-Type": "application/json",
            "X-Shopify-Hmac-Sha256": _sign(_ORDER_WITH_SHIPPING, _WEBHOOK_SECRET),
        }
        with patch(
            "zdrovena.api.routers.webhooks._get_webhook_secret", return_value=_WEBHOOK_SECRET
        ):
            resp = client.post(
                "/api/webhooks/shopify/order-create",
                content=_ORDER_WITH_SHIPPING,
                headers=headers,
            )
        assert resp.status_code == 403

    def test_orders_updated_topic_rejected(self, client):
        """orders/updated used to be whitelisted but is now rejected — the handler
        creates a draft, which would produce unwanted duplicates on every order edit.
        Re-add it once a dedicated update handler exists.
        """
        with patch(
            "zdrovena.api.routers.webhooks._get_webhook_secret", return_value=_WEBHOOK_SECRET
        ):
            resp = client.post(
                "/api/webhooks/shopify/order-create",
                content=_ORDER_WITH_SHIPPING,
                headers=_shopify_headers(
                    _ORDER_WITH_SHIPPING, topic="orders/updated", webhook_id="wh-upd"
                ),
            )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Topic not allowed"

    def test_disallowed_domain_rejected_403(self, client):
        with patch(
            "zdrovena.api.routers.webhooks._get_webhook_secret", return_value=_WEBHOOK_SECRET
        ):
            with patch.dict("os.environ", {"SHOPIFY_ALLOWED_DOMAINS": "zdrovena.myshopify.com"}):
                headers = _shopify_headers(_ORDER_WITH_SHIPPING)
                headers["X-Shopify-Shop-Domain"] = "evil.myshopify.com"
                resp = client.post(
                    "/api/webhooks/shopify/order-create",
                    content=_ORDER_WITH_SHIPPING,
                    headers=headers,
                )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Shop domain not allowed"

    def test_allowed_domain_accepted(self, client):
        with patch(
            "zdrovena.api.routers.webhooks._get_webhook_secret", return_value=_WEBHOOK_SECRET
        ):
            with patch("zdrovena.shipping.application.drafts.create_draft"):
                with patch.dict(
                    "os.environ", {"SHOPIFY_ALLOWED_DOMAINS": "zdrovena.myshopify.com"}
                ):
                    headers = _shopify_headers(_ORDER_WITH_SHIPPING)
                    headers["X-Shopify-Shop-Domain"] = "zdrovena.myshopify.com"
                    resp = client.post(
                        "/api/webhooks/shopify/order-create",
                        content=_ORDER_WITH_SHIPPING,
                        headers=headers,
                    )
        assert resp.status_code == 200
        assert resp.json() == {"status": "accepted"}

    def test_duplicate_webhook_id_returns_duplicate(self, client):
        with patch(
            "zdrovena.api.routers.webhooks._get_webhook_secret", return_value=_WEBHOOK_SECRET
        ):
            with patch("zdrovena.shipping.application.drafts.create_draft") as mock_create:
                headers = _shopify_headers(_ORDER_WITH_SHIPPING, webhook_id="wh-dup-1")
                first = client.post(
                    "/api/webhooks/shopify/order-create",
                    content=_ORDER_WITH_SHIPPING,
                    headers=headers,
                )
                second = client.post(
                    "/api/webhooks/shopify/order-create",
                    content=_ORDER_WITH_SHIPPING,
                    headers=headers,
                )
        assert first.status_code == 200
        assert first.json() == {"status": "accepted"}
        assert second.status_code == 200
        assert second.json() == {"status": "duplicate", "webhook_id": "wh-dup-1"}
        # Second (duplicate) delivery must NOT enqueue a second draft creation.
        assert mock_create.call_count == 1

    def test_missing_webhook_id_still_processes(self, client):
        """No X-Shopify-Webhook-Id → warn and continue (dedup skipped, not a hard error)."""
        with patch(
            "zdrovena.api.routers.webhooks._get_webhook_secret", return_value=_WEBHOOK_SECRET
        ):
            with patch("zdrovena.shipping.application.drafts.create_draft"):
                resp = client.post(
                    "/api/webhooks/shopify/order-create",
                    content=_ORDER_WITH_SHIPPING,
                    headers=_shopify_headers(_ORDER_WITH_SHIPPING, webhook_id=None),
                )
        assert resp.status_code == 200
        assert resp.json() == {"status": "accepted"}

    def test_dedup_store_failure_returns_503(self, client):
        from zdrovena.common.shopify_dedup_store import DedupStoreError

        broken = MagicMock()
        # The endpoint now uses the atomic check-and-set method.
        broken.mark_seen_if_new.side_effect = DedupStoreError("backend down")
        with patch(
            "zdrovena.api.routers.webhooks._get_webhook_secret", return_value=_WEBHOOK_SECRET
        ):
            with patch("zdrovena.api.deps._shopify_dedup_singleton", return_value=broken):
                resp = client.post(
                    "/api/webhooks/shopify/order-create",
                    content=_ORDER_WITH_SHIPPING,
                    headers=_shopify_headers(_ORDER_WITH_SHIPPING, webhook_id="wh-fail"),
                )
        assert resp.status_code == 503
        assert resp.json()["detail"] == "Dedup store unavailable"


# ── List drafts ───────────────────────────────────────────────────────────────


class TestListDrafts:
    def test_empty_returns_empty_list(self, client):
        resp = client.get("/api/shipping/drafts")
        assert resp.status_code == 200
        assert resp.json() == {"drafts": []}

    def test_returns_stored_drafts(self, client, store):
        draft = {
            "id": "abc-123",
            "created_at": "2026-05-20T10:00:00+00:00",
            "source": "shopify",
            "shopify_order_id": "1",
            "shopify_order_number": "1001",
            "customer_name": "Jan Kowalski",
            "courier": "inpost",
            "service": "inpost_courier_standard",
            "tracking_number": "ABC123",
            "courier_draft_id": "d-1",
            "status": "created",
            "packages_count": 1,
            "pickup_ordered": False,
            "receiver": {
                "first_name": "Jan",
                "last_name": "Kowalski",
                "email": "",
                "phone": "",
                "locker_id": "",
            },
            "shipping_address": {"street": "Kwiatowa 1", "city": "Warszawa", "post_code": "00-001"},
            "parcel": {"template": "small", "weight_kg": None},
            "error": None,
        }
        store.upsert_draft(draft)
        resp = client.get("/api/shipping/drafts")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["drafts"]) == 1
        assert data["drafts"][0]["id"] == "abc-123"
        assert data["drafts"][0]["packages_count"] == 1


# ── Execute draft ─────────────────────────────────────────────────────────────


def _seed_error_draft(store, courier="inpost", service="inpost_courier_standard"):
    draft = {
        "id": "draft-exec-1",
        "created_at": "2026-05-20T10:00:00+00:00",
        "source": "shopify",
        "shopify_order_id": "10",
        "shopify_order_number": "1099",
        "customer_name": "Test User",
        "courier": courier,
        "service": service,
        "tracking_number": None,
        "courier_draft_id": None,
        "status": "error",
        "packages_count": 1,
        "pickup_ordered": False,
        "receiver": {
            "first_name": "Test",
            "last_name": "User",
            "email": "t@t.com",
            "phone": "500000000",
            "locker_id": "WAW01A",
        },
        "shipping_address": {"street": "Kwiatowa 1", "city": "Warszawa", "post_code": "00-001"},
        "parcel": {"template": "small", "weight_kg": None},
        "error": "no credentials",
    }
    store.upsert_draft(draft)
    return draft


class TestExecuteDraft:
    def _seed_error_draft(self, store, courier="inpost", service="inpost_courier_standard"):
        return _seed_error_draft(store, courier=courier, service=service)

    def test_404_for_missing_draft(self, client):
        resp = client.post("/api/shipping/drafts/nonexistent/execute")
        assert resp.status_code == 404

    def test_409_for_already_created_draft(self, client, store):
        draft = self._seed_error_draft(store)
        store.update_draft(draft["id"], {"status": "created"})
        resp = client.post(f"/api/shipping/drafts/{draft['id']}/execute")
        assert resp.status_code == 409

    def test_execute_inpost_kurier_calls_client(self, client, store):
        draft = self._seed_error_draft(store, courier="inpost", service="inpost_courier_standard")
        with patch(
            "zdrovena.api.routers.webhooks._run_inpost",
            return_value={
                "courier_draft_id": "new-shipment-id",
                "tracking_number": "TRK999",
                "status": "created",
                "error": None,
            },
        ) as mock_run:
            resp = client.post(f"/api/shipping/drafts/{draft['id']}/execute")
        assert resp.status_code == 200
        mock_run.assert_called_once()
        # Verify store was updated
        updated = store.get_draft(draft["id"])
        assert updated["status"] == "created"
        assert updated["courier_draft_id"] == "new-shipment-id"

    def test_execute_courier_error_returns_502(self, client, store):
        draft = self._seed_error_draft(store)
        with patch("zdrovena.api.routers.webhooks._run_inpost", side_effect=Exception("API down")):
            resp = client.post(f"/api/shipping/drafts/{draft['id']}/execute")
        assert resp.status_code == 502

    def test_execute_failure_lands_in_dlq_for_recovery(self, client, store):
        """A shipment that never left must be recoverable, not silently lost.

        Draft creation had a DLQ but execution did not, so a broken courier
        integration produced zero DLQ entries and zero alerts while every
        courier shipment failed.
        """
        draft = self._seed_error_draft(store)
        with patch("zdrovena.api.routers.webhooks._run_inpost", side_effect=Exception("API down")):
            client.post(f"/api/shipping/drafts/{draft['id']}/execute")

        entries = store.list_dlq()
        assert len(entries) == 1, "the failed execution must be queued for retry"
        assert entries[0]["kind"] == "draft_execution"
        assert entries[0]["draft_id"] == draft["id"]
        assert "API down" in entries[0]["last_error"]

    def test_dlq_write_failure_does_not_mask_the_courier_error(self, client, store):
        """Best-effort: bookkeeping must never swallow the real failure."""
        draft = self._seed_error_draft(store)
        with patch("zdrovena.api.routers.webhooks._run_inpost", side_effect=Exception("API down")):
            with patch.object(store, "enqueue_dlq", side_effect=RuntimeError("table down")):
                resp = client.post(f"/api/shipping/drafts/{draft['id']}/execute")
        assert resp.status_code == 502

    def test_execute_failure_log_names_the_root_cause(self, client, store, caplog):
        """The log line must carry WHY it failed, not just WHICH draft failed.

        Observability regression: the handler logged only the draft id, so
        AppExceptions.OuterMessage in Log Analytics read 'execute_draft failed
        for <uuid>' and the real courier error was recoverable only by parsing
        the raw stack text.
        """
        draft = self._seed_error_draft(store)
        cause = "InPost 400: validation_failed sender.company_name required"
        with caplog.at_level(logging.ERROR, logger="zdrovena.api.routers.webhooks"):
            with patch("zdrovena.api.routers.webhooks._run_inpost", side_effect=Exception(cause)):
                client.post(f"/api/shipping/drafts/{draft['id']}/execute")

        failures = [r for r in caplog.records if "execute_draft failed" in r.getMessage()]
        assert failures, "expected an execute_draft failure log record"
        assert any(cause in r.getMessage() for r in failures), (
            "log message must include the underlying courier error, "
            f"got: {[r.getMessage() for r in failures]}"
        )

    def test_second_execute_after_success_is_409_and_does_not_recall_courier(self, client, store):
        # R5-A: once a draft is created, a repeat execute must be rejected and
        # must NOT call the courier again (no duplicate shipment).
        draft = self._seed_error_draft(store)
        with patch(
            "zdrovena.api.routers.webhooks._run_inpost",
            return_value={
                "courier_draft_id": "ship-1",
                "tracking_number": "TRK1",
                "status": "created",
                "error": None,
            },
        ) as mock_run:
            first = client.post(f"/api/shipping/drafts/{draft['id']}/execute")
            assert first.status_code == 200
            second = client.post(f"/api/shipping/drafts/{draft['id']}/execute")
        assert second.status_code == 409
        mock_run.assert_called_once()  # courier hit exactly once

    def test_execute_on_cancelled_draft_is_409(self, client, store):
        draft = self._seed_error_draft(store)
        store.update_draft(draft["id"], {"status": "cancelled"})
        with patch("zdrovena.api.routers.webhooks._run_inpost") as mock_run:
            resp = client.post(f"/api/shipping/drafts/{draft['id']}/execute")
        assert resp.status_code == 409
        mock_run.assert_not_called()

    def test_execute_failure_leaves_draft_retryable(self, client, store):
        # R5-A: a transient failure releases the claim back to `error`, which is
        # an executable state, so a retry can proceed.
        draft = self._seed_error_draft(store)
        with patch("zdrovena.api.routers.webhooks._run_inpost", side_effect=Exception("API down")):
            client.post(f"/api/shipping/drafts/{draft['id']}/execute")
        assert store.get_draft(draft["id"])["status"] == "error"
        # Retry now succeeds.
        with patch(
            "zdrovena.api.routers.webhooks._run_inpost",
            return_value={
                "courier_draft_id": "ship-2",
                "tracking_number": "TRK2",
                "status": "created",
                "error": None,
            },
        ):
            retry = client.post(f"/api/shipping/drafts/{draft['id']}/execute")
        assert retry.status_code == 200
        assert store.get_draft(draft["id"])["status"] == "created"

    def test_exception_after_claim_before_courier_ends_in_error(self, client, store):
        # #136: a failure between the claim and the courier call (here _get_sender)
        # must not leave the draft stuck in `executing`, and must not call the courier.
        draft = self._seed_error_draft(store)
        with patch(
            "zdrovena.api.routers.webhooks._get_sender", side_effect=RuntimeError("kv down")
        ):
            with patch("zdrovena.api.routers.webhooks._run_inpost") as mock_run:
                resp = client.post(f"/api/shipping/drafts/{draft['id']}/execute")
        assert resp.status_code == 502
        mock_run.assert_not_called()
        assert store.get_draft(draft["id"])["status"] == "error"

    def test_result_write_failure_ends_in_error(self, client, store, monkeypatch):
        # #136: courier succeeded, but persisting the `created` result fails. The
        # draft must not remain `executing` — cleanup returns it to `error`.
        draft = self._seed_error_draft(store)
        original_update = store.update_draft

        def flaky_update(draft_id, fields):
            if fields.get("status") == "created":
                raise RuntimeError("table write failed")
            return original_update(draft_id, fields)

        monkeypatch.setattr(store, "update_draft", flaky_update)
        with patch(
            "zdrovena.api.routers.webhooks._run_inpost",
            return_value={
                "courier_draft_id": "ship-x",
                "tracking_number": "TRKX",
                "status": "created",
                "error": None,
            },
        ):
            resp = client.post(f"/api/shipping/drafts/{draft['id']}/execute")
        assert resp.status_code == 502
        assert store.get_draft(draft["id"])["status"] == "error"
        # Still retryable (error is executable).
        assert store.try_claim_execution(draft["id"]) is True

    def test_cleanup_does_not_clobber_created_after_postwrite_error(self, client, store):
        # #136: an exception AFTER the draft was legitimately written to `created`
        # (here log_event) must NOT reset it — conditional cleanup only touches
        # a still-`executing` draft.
        draft = self._seed_error_draft(store)
        with patch(
            "zdrovena.api.routers.webhooks._run_inpost",
            return_value={
                "courier_draft_id": "ship-y",
                "tracking_number": "TRKY",
                "status": "created",
                "error": None,
            },
        ):
            with patch(
                "zdrovena.api.routers.webhooks.log_event",
                side_effect=RuntimeError("log sink down"),
            ):
                client.post(f"/api/shipping/drafts/{draft['id']}/execute")
        # The created state written before log_event must survive the cleanup.
        assert store.get_draft(draft["id"])["status"] == "created"

    def _seed_allegro_error_draft(self, store, courier, service):
        draft = {
            "id": f"draft-allegro-{courier}",
            "created_at": "2026-06-25T10:00:00+00:00",
            "source": "allegro",
            "external_order_id": "AL-ORDER-77",
            "shopify_order_id": None,
            "shopify_order_number": "AL77",
            "customer_name": "Allegro Buyer",
            "courier": courier,
            "service": service,
            "tracking_number": None,
            "courier_draft_id": None,
            "status": "error",
            "packages_count": 1,
            "pickup_ordered": False,
            "receiver": {
                "first_name": "Allegro",
                "last_name": "Buyer",
                "email": "b@b.com",
                "phone": "600000000",
                "locker_id": "WAW02A",
            },
            "shipping_address": {"street": "Testowa 2", "city": "Kraków", "post_code": "30-001"},
            "parcel": {"template": "small", "weight_kg": None},
            "error": "prev failure",
        }
        store.upsert_draft(draft)
        return draft

    def test_execute_allegro_inpost_pushes_tracking_with_inpost_carrier(self, client, store):
        draft = self._seed_allegro_error_draft(
            store, courier="inpost", service="inpost_courier_standard"
        )
        allegro_client = MagicMock()
        with (
            patch(
                "zdrovena.api.routers.webhooks._run_inpost",
                return_value={
                    "courier_draft_id": "inpost-shipment-77",
                    "tracking_number": "6200XYZ",
                    "status": "created",
                    "error": None,
                },
            ),
            patch(
                "zdrovena.api.routers.webhooks._get_allegro_client",
                return_value=allegro_client,
            ),
        ):
            resp = client.post(f"/api/shipping/drafts/{draft['id']}/execute")
        assert resp.status_code == 200
        allegro_client.create_shipment.assert_called_once_with(
            order_id="AL-ORDER-77",
            carrier_id="INPOST",
            waybill="6200XYZ",
        )

    def test_execute_allegro_apaczka_pushes_tracking_with_other_carrier(self, client, store):
        draft = self._seed_allegro_error_draft(store, courier="apaczka", service="apaczka_courier")
        allegro_client = MagicMock()
        with (
            patch(
                "zdrovena.api.routers.webhooks._run_apaczka",
                return_value={
                    "courier_draft_id": "apaczka-order-88",
                    "tracking_number": "APZWAY0088",
                    "status": "created",
                    "error": None,
                },
            ),
            patch(
                "zdrovena.api.routers.webhooks._get_allegro_client",
                return_value=allegro_client,
            ),
        ):
            resp = client.post(f"/api/shipping/drafts/{draft['id']}/execute")
        assert resp.status_code == 200
        allegro_client.create_shipment.assert_called_once_with(
            order_id="AL-ORDER-77",
            carrier_id="OTHER",
            waybill="APZWAY0088",
        )

    def test_execute_allegro_push_error_does_not_break_execute(self, client, store):
        draft = self._seed_allegro_error_draft(
            store, courier="inpost", service="inpost_courier_standard"
        )
        allegro_client = MagicMock()
        allegro_client.create_shipment.side_effect = RuntimeError("Allegro 500")
        with (
            patch(
                "zdrovena.api.routers.webhooks._run_inpost",
                return_value={
                    "courier_draft_id": "x",
                    "tracking_number": "TRK-OK",
                    "status": "created",
                    "error": None,
                },
            ),
            patch(
                "zdrovena.api.routers.webhooks._get_allegro_client",
                return_value=allegro_client,
            ),
        ):
            resp = client.post(f"/api/shipping/drafts/{draft['id']}/execute")
        assert resp.status_code == 200
        updated = store.get_draft(draft["id"])
        assert updated["tracking_number"] == "TRK-OK"
        assert updated["status"] == "created"

    def test_execute_shopify_draft_does_not_push_to_allegro(self, client, store):
        draft = self._seed_error_draft(store, courier="inpost")
        allegro_client = MagicMock()
        with (
            patch(
                "zdrovena.api.routers.webhooks._run_inpost",
                return_value={
                    "courier_draft_id": "x",
                    "tracking_number": "TRK-SHOPIFY",
                    "status": "created",
                    "error": None,
                },
            ),
            patch(
                "zdrovena.api.routers.webhooks._get_allegro_client",
                return_value=allegro_client,
            ),
        ):
            resp = client.post(f"/api/shipping/drafts/{draft['id']}/execute")
        assert resp.status_code == 200
        allegro_client.create_shipment.assert_not_called()


class TestExecutePreviewEndpoint:
    def test_preview_returns_payload_and_sends_nothing(self, client, store):
        draft = _seed_error_draft(store)
        with patch("zdrovena.api.routers.webhooks._run_inpost") as mock_run:
            resp = client.get(f"/api/shipping/drafts/{draft['id']}/execute/preview")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "sender" in body and "parcels" in body
        assert len(body["fingerprint"]) == 64
        mock_run.assert_not_called()

    def test_preview_404_for_unknown_draft(self, client):
        resp = client.get("/api/shipping/drafts/does-not-exist/execute/preview")
        assert resp.status_code == 404

    def test_preview_shows_one_parcel_per_box_with_its_payload(self, client, store):
        draft = _seed_error_draft(store)
        store.update_draft(draft["id"], {"packages_breakdown": [{"type": "2-pak", "qty": 2}]})
        resp = client.get(f"/api/shipping/drafts/{draft['id']}/execute/preview")
        assert resp.status_code == 200, resp.text
        parcels = resp.json()["parcels"]
        assert len(parcels) == 2
        assert parcels[0]["package_type"] == "2-pak"
        assert parcels[0]["payload"]["service"] == "inpost_courier_standard"
        assert parcels[0]["payload"]["parcels"][0]["weight"]["amount"] == 12.0

    def test_preview_does_not_claim_the_draft_for_execution(self, client, store):
        """A preview that blocks the execute it precedes would be a trap."""
        draft = _seed_error_draft(store)
        client.get(f"/api/shipping/drafts/{draft['id']}/execute/preview")
        assert store.get_draft(draft["id"])["status"] == "error"

    def test_execute_rejects_a_draft_changed_after_preview(self, client, store):
        draft = _seed_error_draft(store)
        preview = client.get(f"/api/shipping/drafts/{draft['id']}/execute/preview").json()
        store.update_draft(
            draft["id"],
            {
                "shipping_address": {
                    **draft["shipping_address"],
                    "street": "Changed after review",
                }
            },
        )

        with patch("zdrovena.api.routers.webhooks._run_inpost") as mock_run:
            resp = client.post(
                f"/api/shipping/drafts/{draft['id']}/execute",
                json={"preview_fingerprint": preview["fingerprint"]},
            )

        assert resp.status_code == 409
        assert "changed after preview" in resp.json()["detail"]
        mock_run.assert_not_called()
        assert store.get_draft(draft["id"])["status"] == "error"

    def test_execute_accepts_the_unchanged_preview_fingerprint(self, client, store):
        draft = _seed_error_draft(store)
        preview = client.get(f"/api/shipping/drafts/{draft['id']}/execute/preview").json()
        with patch(
            "zdrovena.api.routers.webhooks._run_inpost",
            return_value={
                "courier_draft_id": "ship-reviewed",
                "tracking_number": "TRK-REVIEWED",
                "status": "created",
                "error": None,
            },
        ) as mock_run:
            resp = client.post(
                f"/api/shipping/drafts/{draft['id']}/execute",
                json={"preview_fingerprint": preview["fingerprint"]},
            )

        assert resp.status_code == 200, resp.text
        mock_run.assert_called_once()

    def test_preview_for_apaczka_renders_a_real_payload(self, client, store):
        """Apaczka used to get an empty parcel list and an "InPost only" note.
        It is the courier that actually ships today, so it gets a real preview."""
        draft = _seed_error_draft(store, courier="apaczka", service="apaczka_courier")
        store.update_draft(draft["id"], {"apaczka_service_id": "42"})
        with patch("zdrovena.api.routers.webhooks._get_pickup_address", return_value=_PICKUP):
            resp = client.get(f"/api/shipping/drafts/{draft['id']}/execute/preview")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["courier"] == "apaczka"
        assert body["preview_available"] is True
        assert body["parcels"] and "payload" in body["parcels"][0]
        assert "note" not in body


# ── Order pickup ──────────────────────────────────────────────────────────────


_ALLEGRO_PICKUP_ADDRESS = {
    "name": "Zdrovena Magazyn",
    "street": "Magazynowa 41",
    "postalCode": "33-300",
    "city": "Nowy Sacz",
    "countryCode": "PL",
    "email": "magazyn@example.com",
    "phone": "600700800",
}


class TestOrderPickup:
    def test_allegro_pickup_address_uses_configured_collection_address(self):
        configured = {
            "name": "Zdrovena Magazyn",
            "firstname": "",
            "lastname": "Zdrovena Magazyn",
            "street": "Magazynowa",
            "building_number": "41",
            "city": "Nowy Sacz",
            "post_code": "33-300",
            "phone": "600700800",
            "email": "magazyn@example.com",
        }

        with patch("zdrovena.api.routers.webhooks._get_pickup_address", return_value=configured):
            result = webhooks_router._get_allegro_pickup_address()

        assert result == _ALLEGRO_PICKUP_ADDRESS

    def _seed_created_kurier(self, store):
        draft = {
            "id": "draft-pickup-1",
            "created_at": "2026-05-20T10:00:00+00:00",
            "source": "shopify",
            "shopify_order_id": "20",
            "shopify_order_number": "1100",
            "customer_name": "Anna Nowak",
            "courier": "inpost",
            "service": "inpost_courier_standard",
            "tracking_number": "TRK001",
            "courier_draft_id": "ship-id-1",
            "status": "created",
            "packages_count": 1,
            "pickup_ordered": False,
            "receiver": {
                "first_name": "Anna",
                "last_name": "Nowak",
                "email": "a@n.com",
                "phone": "600000000",
                "locker_id": "",
            },
            "shipping_address": {"street": "Różana 3", "city": "Kraków", "post_code": "31-001"},
            "parcel": {"template": "small", "weight_kg": None},
            "error": None,
        }
        store.upsert_draft(draft)
        return draft

    def test_404_for_missing_draft(self, client):
        resp = client.post("/api/shipping/drafts/nonexistent/pickup")
        assert resp.status_code == 404

    def test_pickup_allowed_for_paczkomat_draft(self, client, store):
        # Paczkomat also supports dispatch order (drzwi→paczkomat)
        draft = self._seed_created_kurier(store)
        store.update_draft(draft["id"], {"service": "inpost_locker_standard"})
        with patch(
            "zdrovena.common.inpost.InPostClient.create_dispatch_order", return_value={"id": "d-1"}
        ):
            with patch("zdrovena.api.routers.webhooks.get_secret", return_value="test-value"):
                resp = client.post(f"/api/shipping/drafts/{draft['id']}/pickup")
        assert resp.status_code == 200

    def test_400_for_apaczka_draft(self, client, store):
        """Apaczka's API has no standalone pickup call — service_structure,
        orders and order_send are the whole surface — so a pickup can only ride
        along inside order_send at execute time."""
        draft = self._seed_created_kurier(store)
        store.update_draft(draft["id"], {"courier": "apaczka", "service": "apaczka"})
        resp = client.post(f"/api/shipping/drafts/{draft['id']}/pickup")
        assert resp.status_code == 400

    def test_pickup_ordered_for_allegro_draft(self, store):
        """Ship-with-Allegro exposes pickup-proposals + pickups/create-commands,
        so the same button works there."""
        draft = self._seed_created_kurier(store)
        store.update_draft(
            draft["id"], {"courier": "allegro_delivery", "service": "allegro_delivery"}
        )
        allegro = MagicMock()
        allegro.get_ship_with_allegro_pickup_proposals.return_value = [
            {"date": "2026-08-07", "minTime": "09:00", "maxTime": "13:00"}
        ]
        with (
            patch.object(store, "try_claim_pickup", wraps=store.try_claim_pickup) as claim_pickup,
            patch.object(store, "update_draft", wraps=store.update_draft) as update_draft,
            patch("zdrovena.api.routers.webhooks._get_allegro_client", return_value=allegro),
            patch(
                "zdrovena.api.routers.webhooks._get_allegro_pickup_address",
                return_value=_ALLEGRO_PICKUP_ADDRESS,
            ),
        ):
            result = webhooks_router.order_pickup(draft["id"], store, MagicMock(), None, None, None)

        assert result == {"status": "pickup_ordered", "draft_id": draft["id"]}
        assert store.get_draft(draft["id"])["pickup_ordered"] is True
        claim_pickup.assert_called_once_with(draft["id"])
        assert [
            call
            for call in update_draft.call_args_list
            if call.args == (draft["id"], {"pickup_ordered": False})
        ] == []
        allegro.create_ship_with_allegro_pickup.assert_called_once()
        sent = allegro.create_ship_with_allegro_pickup.call_args.kwargs
        allegro.get_ship_with_allegro_pickup_proposals.assert_called_once_with(
            ["ship-id-1"], address=_ALLEGRO_PICKUP_ADDRESS
        )
        assert sent["address"] == _ALLEGRO_PICKUP_ADDRESS
        assert sent["pickup_time"] == {
            "date": "2026-08-07",
            "minTime": "09:00",
            "maxTime": "13:00",
        }

    def test_missing_allegro_credentials_releases_claim_and_allows_retry(self, client, store):
        draft = self._seed_created_kurier(store)
        store.update_draft(
            draft["id"], {"courier": "allegro_delivery", "service": "allegro_delivery"}
        )
        allegro = MagicMock()

        with (
            patch.object(store, "try_claim_pickup", wraps=store.try_claim_pickup) as claim_pickup,
            patch.object(store, "update_draft", wraps=store.update_draft) as update_draft,
            patch(
                "zdrovena.api.routers.webhooks._get_allegro_client",
                side_effect=[None, allegro],
            ),
            patch(
                "zdrovena.api.routers.webhooks._order_allegro_pickup",
                return_value=True,
            ) as order_allegro_pickup,
        ):
            failed = client.post(f"/api/shipping/drafts/{draft['id']}/pickup")

            assert failed.status_code == 502
            assert failed.json() == {"detail": "Allegro credentials missing"}
            assert store.get_draft(draft["id"])["pickup_ordered"] is False
            order_allegro_pickup.assert_not_called()

            retried = client.post(f"/api/shipping/drafts/{draft['id']}/pickup")

        assert retried.status_code == 200, retried.text
        assert retried.json() == {"status": "pickup_ordered", "draft_id": draft["id"]}
        assert claim_pickup.call_count == 2
        order_allegro_pickup.assert_called_once_with(allegro, "ship-id-1", None)
        assert (
            len(
                [
                    call
                    for call in update_draft.call_args_list
                    if call.args == (draft["id"], {"pickup_ordered": False})
                ]
            )
            == 1
        )

    def test_allegro_pickup_with_no_slot_releases_the_claim(self, store):
        """No slot is not a silent success: the flag must stay false so the
        operator can try another day."""
        draft = self._seed_created_kurier(store)
        store.update_draft(
            draft["id"], {"courier": "allegro_delivery", "service": "allegro_delivery"}
        )
        allegro = MagicMock()
        allegro.get_ship_with_allegro_pickup_proposals.return_value = []
        with (
            patch.object(store, "update_draft", wraps=store.update_draft) as update_draft,
            patch("zdrovena.api.routers.webhooks._get_allegro_client", return_value=allegro),
            patch(
                "zdrovena.api.routers.webhooks._get_allegro_pickup_address",
                return_value=_ALLEGRO_PICKUP_ADDRESS,
            ),
        ):
            with pytest.raises(HTTPException) as exc_info:
                webhooks_router.order_pickup(draft["id"], store, MagicMock(), None, None, None)

        assert exc_info.value.status_code == 409
        assert exc_info.value.detail == "Allegro has no pickup slot available for this shipment"
        assert store.get_draft(draft["id"])["pickup_ordered"] is False
        assert (
            len(
                [
                    call
                    for call in update_draft.call_args_list
                    if call.args == (draft["id"], {"pickup_ordered": False})
                ]
            )
            == 1
        )
        allegro.create_ship_with_allegro_pickup.assert_not_called()

    def test_allegro_provider_failure_releases_the_claim_once(self, store):
        draft = self._seed_created_kurier(store)
        store.update_draft(
            draft["id"], {"courier": "allegro_delivery", "service": "allegro_delivery"}
        )
        allegro = MagicMock()
        allegro.get_ship_with_allegro_pickup_proposals.return_value = [
            {"date": "2026-08-07", "minTime": "09:00", "maxTime": "13:00"}
        ]
        allegro.create_ship_with_allegro_pickup.side_effect = RuntimeError("provider write failed")

        with (
            patch.object(store, "update_draft", wraps=store.update_draft) as update_draft,
            patch("zdrovena.api.routers.webhooks._get_allegro_client", return_value=allegro),
            patch(
                "zdrovena.api.routers.webhooks._get_allegro_pickup_address",
                return_value=_ALLEGRO_PICKUP_ADDRESS,
            ),
        ):
            with pytest.raises(HTTPException) as exc_info:
                webhooks_router.order_pickup(draft["id"], store, MagicMock(), None, None, None)

        assert exc_info.value.status_code == 502
        assert exc_info.value.detail == "Allegro pickup error: provider write failed"
        assert store.get_draft(draft["id"])["pickup_ordered"] is False
        allegro.create_ship_with_allegro_pickup.assert_called_once()
        assert (
            len(
                [
                    call
                    for call in update_draft.call_args_list
                    if call.args == (draft["id"], {"pickup_ordered": False})
                ]
            )
            == 1
        )

    def test_409_when_pickup_already_ordered(self, client, store):
        draft = self._seed_created_kurier(store)
        store.update_draft(draft["id"], {"pickup_ordered": True})
        resp = client.post(f"/api/shipping/drafts/{draft['id']}/pickup")
        assert resp.status_code == 409

    def test_409_when_draft_in_error_state(self, client, store):
        draft = self._seed_created_kurier(store)
        store.update_draft(draft["id"], {"status": "error"})
        resp = client.post(f"/api/shipping/drafts/{draft['id']}/pickup")
        assert resp.status_code == 409

    def test_successful_pickup_sets_flag(self, store):
        draft = self._seed_created_kurier(store)
        with (
            patch.object(store, "try_claim_pickup", wraps=store.try_claim_pickup) as claim_pickup,
            patch.object(store, "update_draft", wraps=store.update_draft) as update_draft,
            patch(
                "zdrovena.common.inpost.InPostClient.create_dispatch_order",
                return_value={"id": "disp-1"},
            ) as create_dispatch,
            patch("zdrovena.api.routers.webhooks.get_secret", return_value="test-value"),
        ):
            result = webhooks_router.order_pickup(draft["id"], store, MagicMock(), None, None, None)
        assert result == {"status": "pickup_ordered", "draft_id": draft["id"]}
        updated = store.get_draft(draft["id"])
        assert updated["pickup_ordered"] is True
        assert updated["dispatch_order_id"] == "disp-1"
        claim_pickup.assert_called_once_with(draft["id"])
        create_dispatch.assert_called_once()
        assert [
            call
            for call in update_draft.call_args_list
            if call.args == (draft["id"], {"pickup_ordered": False})
        ] == []

    def test_manual_pickup_collects_every_parcel_in_one_dispatch(self, client, store):
        """Same defect as the execute path: the standalone "Zamów podjazd"
        button dispatched ``courier_draft_id`` alone, so parcels 2..N were never
        collected."""
        draft = self._seed_created_kurier(store)
        store.update_draft(
            draft["id"],
            {
                "courier_shipments": [
                    {
                        "id": "ship-a",
                        "tracking_number": "620A",
                        "package_type": "1-pak",
                        "package_number": "1",
                    },
                    {
                        "id": "ship-b",
                        "tracking_number": "620B",
                        "package_type": "1-pak",
                        "package_number": "2",
                    },
                ]
            },
        )
        with patch(
            "zdrovena.common.inpost.InPostClient.create_dispatch_order",
            return_value={"id": "disp-multi"},
        ) as mock_disp:
            with patch("zdrovena.api.routers.webhooks.get_secret", return_value="test-value"):
                resp = client.post(f"/api/shipping/drafts/{draft['id']}/pickup")

        assert resp.status_code == 200, resp.text
        mock_disp.assert_called_once()
        assert mock_disp.call_args.args[0] == ["ship-a", "ship-b"]

    def test_manual_pickup_persists_the_dispatch_order_id(self, client, store):
        """Without the id there is nothing to DELETE, so the pickup can never be
        cancelled again."""
        draft = self._seed_created_kurier(store)
        with patch(
            "zdrovena.common.inpost.InPostClient.create_dispatch_order",
            return_value={"id": "disp-77"},
        ):
            with patch("zdrovena.api.routers.webhooks.get_secret", return_value="test-value"):
                resp = client.post(f"/api/shipping/drafts/{draft['id']}/pickup")

        assert resp.status_code == 200, resp.text
        updated = store.get_draft(draft["id"])
        assert updated["dispatch_order_id"] == "disp-77"
        assert updated["pickup_ordered"] is True

    def test_manual_pickup_falls_back_to_courier_draft_id_for_historical_drafts(
        self, client, store
    ):
        """Drafts created before ``courier_shipments`` existed carry only the
        single legacy id, and must still be collectable."""
        draft = self._seed_created_kurier(store)
        store.update_draft(draft["id"], {"courier_shipments": []})
        with patch(
            "zdrovena.common.inpost.InPostClient.create_dispatch_order",
            return_value={"id": "disp-legacy"},
        ) as mock_disp:
            with patch("zdrovena.api.routers.webhooks.get_secret", return_value="test-value"):
                resp = client.post(f"/api/shipping/drafts/{draft['id']}/pickup")

        assert resp.status_code == 200, resp.text
        mock_disp.assert_called_once()
        assert mock_disp.call_args.args[0] == ["ship-id-1"]

    def test_409_when_claim_lost_to_concurrent_request(self, client, store):
        """A second request that races in after the claim but before the
        courier call must be rejected, not silently dispatch a duplicate.
        """
        draft = self._seed_created_kurier(store)
        assert store.try_claim_pickup(draft["id"]) is True  # simulates a winning concurrent request
        resp = client.post(f"/api/shipping/drafts/{draft['id']}/pickup")
        assert resp.status_code == 409

    def test_502_rolls_back_claim_so_retry_is_possible(self, client, store):
        draft = self._seed_created_kurier(store)
        with patch(
            "zdrovena.common.inpost.InPostClient.create_dispatch_order",
            side_effect=RuntimeError("InPost unreachable"),
        ):
            with patch("zdrovena.api.routers.webhooks.get_secret", return_value="test-value"):
                resp = client.post(f"/api/shipping/drafts/{draft['id']}/pickup")
        assert resp.status_code == 502
        updated = store.get_draft(draft["id"])
        assert updated["pickup_ordered"] is False

        # A retry after the courier failure must be able to claim again.
        with patch(
            "zdrovena.common.inpost.InPostClient.create_dispatch_order",
            return_value={"id": "disp-retry"},
        ):
            with patch("zdrovena.api.routers.webhooks.get_secret", return_value="test-value"):
                retry_resp = client.post(f"/api/shipping/drafts/{draft['id']}/pickup")
        assert retry_resp.status_code == 200


# ── Cancel shipment / dispatch (Ship with Allegro) ────────────────────────────


class TestCancelShipmentEndpoint:
    def _seed(self, store, **overrides):
        draft = {
            "id": "draft-cxl-1",
            "created_at": "2026-05-20T10:00:00+00:00",
            "source": "allegro",
            "shopify_order_number": "2200",
            "courier": "allegro_delivery",
            "status": "created",
            "allegro_shipment_id": "ship-42",
        }
        draft.update(overrides)
        store.upsert_draft(draft)
        return draft

    def test_404_for_missing_draft(self, client):
        resp = client.delete("/api/shipping/drafts/nonexistent/shipment")
        assert resp.status_code == 404

    def test_409_when_no_shipment_id(self, client, store):
        draft = self._seed(store, allegro_shipment_id=None)
        resp = client.delete(f"/api/shipping/drafts/{draft['id']}/shipment")
        assert resp.status_code == 409

    def test_successful_cancel_updates_store(self, client, store):
        draft = self._seed(store)
        allegro = MagicMock()
        with patch("zdrovena.api.routers.webhooks._get_allegro_client", return_value=allegro):
            resp = client.delete(f"/api/shipping/drafts/{draft['id']}/shipment")
        assert resp.status_code == 200
        assert resp.json()["status"] == "cancelled"
        allegro.cancel_ship_with_allegro_shipment.assert_called_once()
        assert (
            allegro.cancel_ship_with_allegro_shipment.call_args.kwargs["shipment_id"] == "ship-42"
        )
        updated = store.get_draft(draft["id"])
        assert updated["status"] == "cancelled"
        assert updated["allegro_shipment_id"] is None

    def test_falls_back_to_courier_draft_id(self, client, store):
        draft = self._seed(store, allegro_shipment_id=None, courier_draft_id="cd-9")
        allegro = MagicMock()
        with patch("zdrovena.api.routers.webhooks._get_allegro_client", return_value=allegro):
            resp = client.delete(f"/api/shipping/drafts/{draft['id']}/shipment")
        assert resp.status_code == 200
        assert allegro.cancel_ship_with_allegro_shipment.call_args.kwargs["shipment_id"] == "cd-9"

    def test_502_on_allegro_error(self, client, store):
        from zdrovena.common.shipping_exceptions import AllegroBusinessError

        draft = self._seed(store)
        allegro = MagicMock()
        allegro.cancel_ship_with_allegro_shipment.side_effect = AllegroBusinessError(
            detail="already dispatched", action="cancel-commands"
        )
        with patch("zdrovena.api.routers.webhooks._get_allegro_client", return_value=allegro):
            resp = client.delete(f"/api/shipping/drafts/{draft['id']}/shipment")
        assert resp.status_code == 502


class TestCancelDispatchEndpoint:
    def _seed(self, store, **overrides):
        draft = {
            "id": "draft-cxl-disp-1",
            "created_at": "2026-05-20T10:00:00+00:00",
            "source": "allegro",
            "shopify_order_number": "2300",
            "courier": "allegro_delivery",
            "status": "created",
            "pickup_ordered": True,
            "allegro_dispatch_id": "disp-9",
        }
        draft.update(overrides)
        store.upsert_draft(draft)
        return draft

    def test_404_for_missing_draft(self, client):
        resp = client.delete("/api/shipping/drafts/nonexistent/dispatch")
        assert resp.status_code == 404

    def test_409_when_no_dispatch_id(self, client, store):
        draft = self._seed(store, allegro_dispatch_id=None)
        resp = client.delete(f"/api/shipping/drafts/{draft['id']}/dispatch")
        assert resp.status_code == 409

    def test_successful_cancel_updates_store(self, client, store):
        draft = self._seed(store)
        allegro = MagicMock()
        with patch("zdrovena.api.routers.webhooks._get_allegro_client", return_value=allegro):
            resp = client.delete(f"/api/shipping/drafts/{draft['id']}/dispatch")
        assert resp.status_code == 200
        assert resp.json()["status"] == "dispatch_cancelled"
        allegro.cancel_ship_with_allegro_dispatch.assert_called_once()
        assert allegro.cancel_ship_with_allegro_dispatch.call_args.kwargs["dispatch_id"] == "disp-9"
        updated = store.get_draft(draft["id"])
        assert updated["pickup_ordered"] is False
        assert updated["allegro_dispatch_id"] is None

    def test_502_on_allegro_error(self, client, store):
        from zdrovena.common.shipping_exceptions import AllegroBusinessError

        draft = self._seed(store)
        allegro = MagicMock()
        allegro.cancel_ship_with_allegro_dispatch.side_effect = AllegroBusinessError(
            detail="already accepted", action="cancel-commands"
        )
        with patch("zdrovena.api.routers.webhooks._get_allegro_client", return_value=allegro):
            resp = client.delete(f"/api/shipping/drafts/{draft['id']}/dispatch")
        assert resp.status_code == 502


# ── Manual fulfillment marking ──────────────────────────────────────────────
# The old ``mark-allegro-processed`` endpoint was replaced by the generic
# ``mark-fulfilled`` endpoint (PR #77). Dedicated coverage now lives in
# ``tests/test_mark_fulfilled_endpoint.py``; kept here only as a locator.


# ── Update packages_count ─────────────────────────────────────────────────────


class TestUpdateDraft:
    def _seed_draft(self, store):
        draft = {
            "id": "draft-upd-1",
            "created_at": "2026-05-20T10:00:00+00:00",
            "source": "shopify",
            "shopify_order_id": "30",
            "shopify_order_number": "1200",
            "customer_name": "Piotr Wróbel",
            "courier": "apaczka",
            "service": "apaczka",
            "tracking_number": None,
            "courier_draft_id": "ap-1",
            "status": "created",
            "packages_count": 1,
            "pickup_ordered": False,
            "receiver": {
                "first_name": "Piotr",
                "last_name": "Wróbel",
                "email": "",
                "phone": "",
                "locker_id": "",
            },
            "shipping_address": {"street": "Wiśniowa 5", "city": "Gdańsk", "post_code": "80-001"},
            "parcel": {"template": "small", "weight_kg": 1.0},
            "error": None,
        }
        store.upsert_draft(draft)
        return draft

    def test_404_for_missing_draft(self, client):
        resp = client.patch("/api/shipping/drafts/nonexistent", json={"packages_count": 2})
        assert resp.status_code == 404

    def test_updates_packages_count(self, client, store):
        draft = self._seed_draft(store)
        resp = client.patch(f"/api/shipping/drafts/{draft['id']}", json={"packages_count": 3})
        assert resp.status_code == 200
        assert resp.json()["packages_count"] == 3
        updated = store.get_draft(draft["id"])
        assert updated["packages_count"] == 3

    def test_rejects_zero_count(self, client, store):
        draft = self._seed_draft(store)
        resp = client.patch(f"/api/shipping/drafts/{draft['id']}", json={"packages_count": 0})
        assert resp.status_code == 422

    def test_rejects_count_above_99(self, client, store):
        draft = self._seed_draft(store)
        resp = client.patch(f"/api/shipping/drafts/{draft['id']}", json={"packages_count": 100})
        assert resp.status_code == 422

    def test_reviewed_true_clears_needs_review_status(self, client, store):
        draft = self._seed_draft(store)
        store.update_draft(draft["id"], {"status": "needs_review"})
        resp = client.patch(f"/api/shipping/drafts/{draft['id']}", json={"reviewed": True})
        assert resp.status_code == 200
        assert resp.json()["status"] == "pending"
        updated = store.get_draft(draft["id"])
        assert updated["status"] == "pending"

    def test_reviewed_true_clears_error_field(self, client, store):
        draft = self._seed_draft(store)
        store.update_draft(draft["id"], {"status": "needs_review", "error": "Test error"})
        resp = client.patch(f"/api/shipping/drafts/{draft['id']}", json={"reviewed": True})
        assert resp.status_code == 200
        assert resp.json()["error"] is None
        updated = store.get_draft(draft["id"])
        assert updated["error"] is None

    def test_reviewed_true_ignored_when_not_needs_review(self, client, store):
        draft = self._seed_draft(store)
        store.update_draft(draft["id"], {"status": "pending"})
        resp = client.patch(f"/api/shipping/drafts/{draft['id']}", json={"reviewed": True})
        assert resp.status_code == 200
        assert resp.json()["status"] == "pending"

    def test_needs_review_draft_still_blocks_execute(self, client, store):
        draft = self._seed_draft(store)
        store.update_draft(draft["id"], {"status": "needs_review"})
        resp = client.post(f"/api/shipping/drafts/{draft['id']}/execute")
        assert resp.status_code == 409
        assert "requires review" in resp.json()["detail"].lower()

    def test_sets_apaczka_service_id(self, client, store):
        draft = self._seed_draft(store)
        resp = client.patch(
            f"/api/shipping/drafts/{draft['id']}", json={"apaczka_service_id": "21"}
        )
        assert resp.status_code == 200
        assert resp.json()["apaczka_service_id"] == "21"
        assert resp.json()["shipping_service_match_status"] == "manual"
        assert resp.json()["shipping_service_match_source"] == "operator"
        updated = store.get_draft(draft["id"])
        assert updated["apaczka_service_id"] == "21"
        assert updated["shipping_service_match_detail"] == "Manual Apaczka service override"

    def test_rejects_unknown_apaczka_service_id(self, client, store):
        draft = self._seed_draft(store)
        resp = client.patch(
            f"/api/shipping/drafts/{draft['id']}", json={"apaczka_service_id": "999999"}
        )
        assert resp.status_code == 400
        assert "apaczka_service_id" in resp.json()["detail"].lower()

    def test_apaczka_service_id_does_not_auto_clear_needs_review(self, client, store):
        """Matches existing service/locker_id behavior: setting the field
        alone does not flip status — the operator still confirms separately
        via reviewed=True."""
        draft = self._seed_draft(store)
        store.update_draft(draft["id"], {"status": "needs_review"})
        resp = client.patch(
            f"/api/shipping/drafts/{draft['id']}", json={"apaczka_service_id": "21"}
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "needs_review"

    def test_apaczka_service_id_and_reviewed_together_clears_needs_review(self, client, store):
        draft = self._seed_draft(store)
        store.update_draft(draft["id"], {"status": "needs_review"})
        resp = client.patch(
            f"/api/shipping/drafts/{draft['id']}",
            json={"apaczka_service_id": "21", "reviewed": True},
        )
        assert resp.status_code == 200
        assert resp.json()["apaczka_service_id"] == "21"
        assert resp.json()["status"] == "pending"

    def test_after_reviewed_execute_not_blocked_by_review(self, client, store):
        draft = self._seed_draft(store)
        store.update_draft(draft["id"], {"status": "needs_review"})
        # First PATCH to mark as reviewed
        resp = client.patch(f"/api/shipping/drafts/{draft['id']}", json={"reviewed": True})
        assert resp.status_code == 200
        # Now execute should not be blocked by needs_review (no 409 with "review" in message)
        resp = client.post(
            f"/api/shipping/drafts/{draft['id']}/execute",
            json={"pickup_date": "2026-07-05", "pickup_from": "08:00", "pickup_to": "17:00"},
        )
        # Should NOT return 409 with "requires review" message
        if resp.status_code == 409:
            assert "review" not in resp.json()["detail"].lower()


# ── Helper function unit tests ────────────────────────────────────────────────


_SENDER = {
    "name": "Zdrovena",
    "firstname": "",
    "lastname": "Zdrovena",
    "street": "Testowa 1",
    "building_number": "1",
    "city": "Warszawa",
    "post_code": "00-001",
    "phone": "500000000",
    "email": "sender@zdrovena.pl",
}

_KURIER_DRAFT = {
    "id": "d-kurier",
    "shopify_order_number": "1050",
    "courier": "inpost",
    "service": "inpost_courier_standard",
    "receiver": {
        "first_name": "Jan",
        "last_name": "Kowalski",
        "email": "jan@k.pl",
        "phone": "600100200",
        "locker_id": "",
    },
    "shipping_address": {"street": "Kwiatowa 1", "city": "Warszawa", "post_code": "00-001"},
}

_PACZKOMAT_DRAFT = {
    "id": "d-paczkomat",
    "shopify_order_number": "1051",
    "courier": "inpost",
    "service": "inpost_locker_standard",
    "receiver": {
        "first_name": "Anna",
        "last_name": "Nowak",
        "email": "anna@n.pl",
        "phone": "700200300",
        "locker_id": "WAW01A",
    },
    "shipping_address": {"street": "", "city": "", "post_code": ""},
}


class TestPhysicalParcelsCharacterization:
    def test_missing_breakdown_falls_back_to_one_1pak(self):
        parcels = physical_parcels({})

        assert parcels == [PhysicalParcel(package_type="1-pak", position=1, count_for_type=1)]

    def test_empty_breakdown_falls_back_to_one_1pak(self):
        parcels = physical_parcels({"packages_breakdown": []})

        assert parcels == [PhysicalParcel(package_type="1-pak", position=1, count_for_type=1)]

    def test_mixed_package_types_preserve_input_order(self):
        draft = {
            "packages_breakdown": [
                {"type": "2-pak", "qty": 1},
                {"type": "szkło", "qty": 1},
                {"type": "pół-pak", "qty": 1},
            ]
        }

        parcels = physical_parcels(draft)

        assert parcels == [
            PhysicalParcel(package_type="2-pak", position=1, count_for_type=1),
            PhysicalParcel(package_type="szkło", position=1, count_for_type=1),
            PhysicalParcel(package_type="pół-pak", position=1, count_for_type=1),
        ]

    def test_repeated_package_type_expands_quantity(self):
        draft = {"packages_breakdown": [{"type": "1-pak", "qty": 3}]}

        parcels = physical_parcels(draft)

        assert parcels == [
            PhysicalParcel(package_type="1-pak", position=1, count_for_type=3),
            PhysicalParcel(package_type="1-pak", position=2, count_for_type=3),
            PhysicalParcel(package_type="1-pak", position=3, count_for_type=3),
        ]

    def test_numbering_restarts_for_each_package_type(self):
        draft = {
            "packages_breakdown": [
                {"type": "2-pak", "qty": 2},
                {"type": "szkło", "qty": 2},
            ]
        }

        parcels = physical_parcels(draft)

        assert parcels == [
            PhysicalParcel(package_type="2-pak", position=1, count_for_type=2),
            PhysicalParcel(package_type="2-pak", position=2, count_for_type=2),
            PhysicalParcel(package_type="szkło", position=1, count_for_type=2),
            PhysicalParcel(package_type="szkło", position=2, count_for_type=2),
        ]

    def test_string_quantity_is_coerced_to_int(self):
        draft = {"packages_breakdown": [{"type": "3-pak", "qty": "2"}]}

        parcels = physical_parcels(draft)

        assert parcels == [
            PhysicalParcel(package_type="3-pak", position=1, count_for_type=2),
            PhysicalParcel(package_type="3-pak", position=2, count_for_type=2),
        ]

    def test_blank_type_falls_back_but_unknown_type_is_preserved(self):
        draft = {
            "packages_breakdown": [
                {"type": "", "qty": 1},
                {"type": "custom-box", "qty": 1},
            ]
        }

        parcels = physical_parcels(draft)

        assert parcels == [
            PhysicalParcel(package_type="1-pak", position=1, count_for_type=1),
            PhysicalParcel(package_type="custom-box", position=1, count_for_type=1),
        ]


class TestParcelWeightAndDimsCharacterization:
    def test_empty_breakdown_uses_default_weight_and_dimensions(self):
        from zdrovena.common.inpost import PARCEL_SPECS

        weight, dimensions = parcel_weight_and_dims({"packages_breakdown": []})

        assert weight == 6.0
        assert dimensions is PARCEL_SPECS["1-pak"]

    def test_unknown_only_breakdown_uses_default_weight_and_dimensions(self):
        from zdrovena.common.inpost import PARCEL_SPECS

        weight, dimensions = parcel_weight_and_dims(
            {"packages_breakdown": [{"type": "custom-box", "qty": 9}]}
        )

        assert weight == 6.0
        assert dimensions is PARCEL_SPECS["1-pak"]

    def test_quantity_multiplies_package_weight(self):
        weight, dimensions = parcel_weight_and_dims(
            {"packages_breakdown": [{"type": "pół-pak", "qty": 3}]}
        )

        assert weight == 9.0
        assert dimensions == {
            "length": 20,
            "width": 15,
            "height": 20,
            "weight_kg": 3.0,
            "paczkomat_template": "large",
        }

    def test_largest_dimensions_are_selected_by_volume_not_quantity(self):
        weight, dimensions = parcel_weight_and_dims(
            {
                "packages_breakdown": [
                    {"type": "pół-pak", "qty": 10},
                    {"type": "2-pak", "qty": 1},
                ]
            }
        )

        assert weight == 42.0
        assert dimensions == {
            "length": 40,
            "width": 30,
            "height": 20,
            "weight_kg": 12.0,
            "paczkomat_template": "large",
        }

    def test_returned_dimensions_keep_exact_legacy_dictionary_shape(self):
        weight, dimensions = parcel_weight_and_dims(
            {"packages_breakdown": [{"type": "szkło", "qty": 1}]}
        )

        assert isinstance(weight, float)
        assert isinstance(dimensions, dict)
        assert dimensions == {
            "length": 30,
            "width": 30,
            "height": 20,
            "weight_kg": 9.0,
            "paczkomat_template": "large",
        }
        assert list(dimensions) == [
            "length",
            "width",
            "height",
            "weight_kg",
            "paczkomat_template",
        ]


class TestShipmentReferenceCharacterization:
    def test_single_parcel_has_no_numbering_suffix(self):
        assert shipment_reference("1050", "1-pak", 1, 1) == "1050 | plastik | 1-pak"

    def test_repeated_same_type_parcels_have_numbering_suffix(self):
        assert shipment_reference("1050", "2-pak", 1, 2) == "1050 | plastik | 2-pak 1/2"
        assert shipment_reference("1050", "2-pak", 2, 2) == "1050 | plastik | 2-pak 2/2"

    def test_mixed_single_package_types_do_not_get_global_numbering(self):
        references = [
            shipment_reference("1050", "3-pak", 1, 1),
            shipment_reference("1050", "1-pak", 1, 1),
        ]

        assert references == [
            "1050 | plastik | 3-pak",
            "1050 | plastik | 1-pak",
        ]

    @pytest.mark.parametrize(
        ("package_type", "expected"),
        [
            ("szkło", "1050 | szkło | 1-pak"),
            ("szkło-2pak", "1050 | szkło | 2-pak"),
        ],
    )
    def test_glass_package_type_formats_material_and_size(self, package_type, expected):
        assert shipment_reference("1050", package_type, 1, 1) == expected

    def test_unknown_package_type_is_formatted_as_plastic_without_normalization(self):
        assert shipment_reference("1050", "custom-box", 1, 1) == "1050 | plastik | custom-box"


class TestParcelDomainShapes:
    def test_typed_parcels_keep_explicit_legacy_conversion(self):
        parcels = physical_parcels({"packages_breakdown": [{"type": "1-pak", "qty": 1}]})
        legacy_parcels = [parcel.to_legacy_tuple() for parcel in parcels]
        summary = parcel_weight_and_dims({"packages_breakdown": [{"type": "1-pak", "qty": 1}]})
        reference = shipment_reference("1050", "1-pak", 1, 1)

        assert isinstance(parcels, list)
        assert all(isinstance(parcel, PhysicalParcel) for parcel in parcels)
        assert legacy_parcels == [("1-pak", 1, 1)]
        assert isinstance(summary, tuple)
        assert isinstance(summary[0], float)
        assert isinstance(summary[1], dict)
        assert isinstance(reference, str)


class TestParcelCatalogCompatibility:
    def test_existing_catalog_entry_is_the_same_mutable_object_used_by_planning(self):
        from zdrovena.common.inpost import _DEFAULT_DIMS, PARCEL_SPECS

        draft = {
            **_KURIER_DRAFT,
            "packages_breakdown": [{"type": "2-pak", "qty": 1}],
        }
        call_specs = inpost_call_specs(draft, _SENDER)
        _weight, dimensions = parcel_weight_and_dims(draft)

        assert _DEFAULT_DIMS is PARCEL_SPECS["1-pak"]
        assert dimensions is PARCEL_SPECS["2-pak"]
        assert call_specs[0][4]["dimensions"] is PARCEL_SPECS["2-pak"]

    def test_monkeypatching_existing_catalog_path_changes_current_planning_paths(self, monkeypatch):
        from zdrovena.common import inpost

        patched_spec = {
            "length": 11,
            "width": 12,
            "height": 13,
            "weight_kg": 4.5,
            "paczkomat_template": "medium",
        }
        monkeypatch.setitem(inpost.PARCEL_SPECS, "custom-box", patched_spec)
        draft = {
            **_KURIER_DRAFT,
            "packages_breakdown": [{"type": "custom-box", "qty": 2}],
        }

        weight, dimensions = parcel_weight_and_dims(draft)
        plan = _provider_inpost_payload_plan(draft, _SENDER)

        assert weight == 9.0
        assert dimensions is patched_spec
        assert plan[0]["payload"]["parcels"][0] == {
            "dimensions": {
                "unit": "mm",
                "length": 110,
                "width": 120,
                "height": 130,
            },
            "weight": {"unit": "kg", "amount": 4.5},
        }


class TestRunInpost:
    def test_kurier_execute_creates_shipment_without_dispatch(self):
        from zdrovena.api.routers.webhooks import _run_inpost

        with patch("zdrovena.api.routers.webhooks.get_secret", return_value="tok"):
            with patch("zdrovena.common.inpost.InPostClient.create_kurier_shipment") as mock_ship:
                with patch(
                    "zdrovena.common.inpost.InPostClient.create_dispatch_order"
                ) as mock_disp:
                    mock_ship.return_value = {"id": "ship-1", "tracking_number": "TRK1"}
                    mock_disp.return_value = {"id": "disp-1"}
                    result = _run_inpost(_KURIER_DRAFT, _SENDER)
        assert result["courier_draft_id"] == "ship-1"
        assert result["tracking_number"] == "TRK1"
        assert result["status"] == "created"
        assert result["pickup_ordered"] is False
        assert result["dispatch_order_id"] is None
        mock_disp.assert_not_called()

    def test_kurier_execute_does_not_attempt_dispatch(self):
        from zdrovena.api.routers.webhooks import _run_inpost

        with patch("zdrovena.api.routers.webhooks.get_secret", return_value="tok"):
            with patch("zdrovena.common.inpost.InPostClient.create_kurier_shipment") as mock_ship:
                with patch(
                    "zdrovena.common.inpost.InPostClient.create_dispatch_order",
                    side_effect=Exception("dispatch fail"),
                ) as mock_disp:
                    mock_ship.return_value = {"id": "ship-2", "tracking_number": "TRK2"}
                    result = _run_inpost(_KURIER_DRAFT, _SENDER)
        assert result["status"] == "created"
        assert result["pickup_ordered"] is False
        mock_disp.assert_not_called()

    def test_pickup_window_orders_the_dispatch_at_execute(self):
        """One pickup control for every carrier. Apaczka's API has no pickup
        resource, so a collection can only be requested inside order_send at
        execute time — which makes execute the only moment all three carriers
        share. InPost used to accept the window and silently drop it, leaving
        the parcel to sit uncollected."""
        from zdrovena.api.routers.webhooks import _run_inpost

        with patch("zdrovena.api.routers.webhooks.get_secret", return_value="tok"):
            with patch("zdrovena.common.inpost.InPostClient.create_kurier_shipment") as mock_ship:
                with patch(
                    "zdrovena.common.inpost.InPostClient.create_dispatch_order"
                ) as mock_disp:
                    mock_ship.return_value = {"id": "ship-1", "tracking_number": "TRK1"}
                    mock_disp.return_value = {"id": "disp-9"}
                    result = _run_inpost(
                        _KURIER_DRAFT,
                        _SENDER,
                        pickup_date="2026-08-08",
                        pickup_from="09:00",
                        pickup_to="13:00",
                    )

        mock_disp.assert_called_once()
        assert mock_disp.call_args.args[0] == ["ship-1"]
        assert mock_disp.call_args.kwargs["pickup_date"] == "2026-08-08"
        assert mock_disp.call_args.kwargs["pickup_from"] == "09:00"
        assert mock_disp.call_args.kwargs["pickup_to"] == "13:00"
        assert result["pickup_ordered"] is True
        assert result["dispatch_order_id"] == "disp-9"

    def test_pickup_collects_every_physical_parcel_in_one_dispatch(self):
        """Production incident: a two-parcel order created both shipments and
        both labels, but the dispatch was built from ``courier_draft_id`` — the
        first parcel only — so the courier collected one box and the second sat
        in the warehouse with a valid waybill on it."""
        from zdrovena.api.routers.webhooks import _run_inpost

        draft = {
            **_KURIER_DRAFT,
            "packages_breakdown": [{"type": "1-pak", "qty": 2}],
        }
        with patch("zdrovena.api.routers.webhooks.get_secret", return_value="tok"):
            with patch("zdrovena.common.inpost.InPostClient.create_kurier_shipment") as mock_ship:
                with patch(
                    "zdrovena.common.inpost.InPostClient.create_dispatch_order"
                ) as mock_disp:
                    mock_ship.side_effect = [
                        {"id": "ship-a", "tracking_number": "620A"},
                        {"id": "ship-b", "tracking_number": "620B"},
                    ]
                    mock_disp.return_value = {"id": "disp-multi"}
                    result = _run_inpost(
                        draft,
                        _SENDER,
                        pickup_date="2026-08-08",
                        pickup_from="09:00",
                        pickup_to="13:00",
                    )

        assert [shipment["id"] for shipment in result["courier_shipments"]] == [
            "ship-a",
            "ship-b",
        ]
        # One collection, one time window — not one dispatch per box.
        mock_disp.assert_called_once()
        assert mock_disp.call_args.args[0] == ["ship-a", "ship-b"]
        assert result["dispatch_order_id"] == "disp-multi"
        assert result["pickup_ordered"] is True

    def test_failed_dispatch_does_not_fail_the_execute(self):
        """The shipment already exists, so a pickup failure must not undo it.
        pickup_ordered stays False, which is what keeps 'Zamów podjazd'
        available as the retry."""
        from zdrovena.api.routers.webhooks import _run_inpost

        with patch("zdrovena.api.routers.webhooks.get_secret", return_value="tok"):
            with patch("zdrovena.common.inpost.InPostClient.create_kurier_shipment") as mock_ship:
                with patch(
                    "zdrovena.common.inpost.InPostClient.create_dispatch_order",
                    side_effect=RuntimeError("InPost dispatch down"),
                ):
                    mock_ship.return_value = {"id": "ship-1", "tracking_number": "TRK1"}
                    result = _run_inpost(
                        _KURIER_DRAFT, _SENDER, pickup_date="2026-08-08", pickup_from="09:00"
                    )

        assert result["courier_draft_id"] == "ship-1"
        assert result["tracking_number"] == "TRK1"
        assert result["pickup_ordered"] is False
        assert result["dispatch_order_id"] is None

    def test_async_create_parks_at_pending_without_blocking_on_confirmation(self):
        """Creation stays non-blocking — no polling inside the request — but a
        shipment with no tracking number is not "nadane" yet. It parks at
        pending_confirmation so the operator sees "waiting", and the confirm
        endpoint (or its 5s UI poll) promotes it once ShipX answers."""
        from zdrovena.api.routers.webhooks import _run_inpost

        with patch("zdrovena.api.routers.webhooks.get_secret", return_value="tok"):
            with patch(
                "zdrovena.common.inpost.InPostClient.create_kurier_shipment",
                return_value={"id": "ship-async", "status": "created", "tracking_number": None},
            ):
                with patch(
                    "zdrovena.common.inpost.InPostClient.wait_for_shipment_confirmation",
                ) as wait:
                    result = _run_inpost(_KURIER_DRAFT, _SENDER)

        wait.assert_not_called()
        assert result["courier_draft_id"] == "ship-async"
        assert result["status"] == "pending_confirmation"
        assert not result["tracking_number"]

    def test_create_with_tracking_is_immediately_created(self):
        """The other half of the contract: when ShipX answers with a tracking
        number straight away there is nothing to wait for."""
        from zdrovena.api.routers.webhooks import _run_inpost

        with patch("zdrovena.api.routers.webhooks.get_secret", return_value="tok"):
            with patch(
                "zdrovena.common.inpost.InPostClient.create_kurier_shipment",
                return_value={"id": "ship-sync", "tracking_number": "620SYNC"},
            ):
                result = _run_inpost(_KURIER_DRAFT, _SENDER)

        assert result["status"] == "created"
        assert result["tracking_number"] == "620SYNC"

    def test_multi_parcel_stays_pending_until_every_parcel_has_tracking(self):
        """A draft is only fully sent when every physical parcel has a waybill;
        one confirmed parcel must not mark the whole order as nadane."""
        from zdrovena.api.routers.webhooks import _run_inpost

        draft = {
            **_KURIER_DRAFT,
            "packages_breakdown": [{"type": "1-pak", "qty": 2}],
        }
        with patch("zdrovena.api.routers.webhooks.get_secret", return_value="tok"):
            with patch("zdrovena.common.inpost.InPostClient.create_kurier_shipment") as mock_ship:
                mock_ship.side_effect = [
                    {"id": "ship-a", "tracking_number": "620A"},
                    {"id": "ship-b", "tracking_number": None},
                ]
                result = _run_inpost(draft, _SENDER)

        assert result["status"] == "pending_confirmation"

    def test_pending_retry_reuses_shipx_id_instead_of_sending_second_post(self):
        from zdrovena.api.routers.webhooks import _run_inpost

        draft = {
            **_KURIER_DRAFT,
            "status": "pending_confirmation",
            "courier_draft_id": "ship-existing",
        }
        with patch("zdrovena.api.routers.webhooks.get_secret", return_value="tok"):
            with patch("zdrovena.common.inpost.InPostClient.create_kurier_shipment") as create:
                with patch(
                    "zdrovena.common.inpost.InPostClient.get_shipment",
                    return_value={
                        "id": "ship-existing",
                        "status": "created",
                        "tracking_number": None,
                    },
                ) as get:
                    with patch(
                        "zdrovena.common.inpost.InPostClient.wait_for_shipment_confirmation",
                        return_value={
                            "id": "ship-existing",
                            "status": "confirmed",
                            "tracking_number": "620EXISTING",
                        },
                    ):
                        result = _run_inpost(draft, _SENDER)

        create.assert_not_called()
        get.assert_called_once_with("ship-existing")
        assert result["courier_draft_id"] == "ship-existing"
        assert result["tracking_number"] == "620EXISTING"

    def test_pending_retry_refreshes_every_unconfirmed_parcel_through_shared_resume(self):
        from zdrovena.api.routers.webhooks import _run_inpost

        draft = {
            **_KURIER_DRAFT,
            "status": "pending_confirmation",
            "courier_draft_id": "ship-1",
            "dispatch_order_id": "dispatch-123",
            "pickup_ordered": True,
            "courier_shipments": [
                {
                    "id": "ship-1",
                    "tracking_number": "TRACK-1",
                    "package_type": "1-pak",
                    "package_number": "1",
                },
                {
                    "id": "ship-2",
                    "tracking_number": "",
                    "package_type": "1-pak",
                    "package_number": "2",
                },
            ],
        }
        with patch("zdrovena.api.routers.webhooks.get_secret", return_value="tok"):
            with patch("zdrovena.common.inpost.InPostClient.create_kurier_shipment") as create:
                with patch(
                    "zdrovena.common.inpost.InPostClient.get_shipment",
                    return_value={"id": "ship-2", "tracking_number": "TRACK-2"},
                ) as get:
                    result = _run_inpost(draft, _SENDER)

        create.assert_not_called()
        get.assert_called_once_with("ship-2")
        assert [shipment["tracking_number"] for shipment in result["courier_shipments"]] == [
            "TRACK-1",
            "TRACK-2",
        ]
        assert result["status"] == "created"
        assert result["dispatch_order_id"] == "dispatch-123"
        assert result["pickup_ordered"] is True

    def test_paczkomat_creates_shipment(self):
        from zdrovena.api.routers.webhooks import _run_inpost

        with patch("zdrovena.api.routers.webhooks.get_secret", return_value="tok"):
            with patch(
                "zdrovena.common.inpost.InPostClient.create_paczkomat_shipment"
            ) as mock_ship:
                mock_ship.return_value = {"id": "pack-1", "tracking_number": "TRKP1"}
                result = _run_inpost(_PACZKOMAT_DRAFT, _SENDER)
        assert result["courier_draft_id"] == "pack-1"
        mock_ship.assert_called_once()
        kw = mock_ship.call_args.kwargs
        assert kw["target_point"] == "WAW01A"

    def test_kurier_building_and_flat_number_joined_with_slash(self):
        from zdrovena.api.routers.webhooks import _run_inpost

        draft = {
            **_KURIER_DRAFT,
            "shipping_address": {
                "street": "Kwiatowa",
                "building_number": "24",
                "flat_number": "5",
                "city": "Warszawa",
                "post_code": "00-001",
            },
        }
        with patch("zdrovena.api.routers.webhooks.get_secret", return_value="tok"):
            with patch("zdrovena.common.inpost.InPostClient.create_kurier_shipment") as mock_ship:
                with patch("zdrovena.common.inpost.InPostClient.create_dispatch_order"):
                    mock_ship.return_value = {"id": "ship-3", "tracking_number": "TRK3"}
                    _run_inpost(draft, _SENDER)
        kw = mock_ship.call_args.kwargs
        assert kw["receiver_building_number"] == "24/5"

    def test_each_physical_box_creates_a_typed_shipment(self):
        from zdrovena.api.routers.webhooks import _run_inpost

        draft = {
            **_KURIER_DRAFT,
            "packages_breakdown": [{"type": "3-pak", "qty": 1}, {"type": "1-pak", "qty": 1}],
        }
        persisted: list[dict[str, str]] = []
        with patch("zdrovena.api.routers.webhooks.get_secret", return_value="tok"):
            with patch("zdrovena.common.inpost.InPostClient.create_kurier_shipment") as mock_ship:
                mock_ship.side_effect = [
                    {"id": "ship-3pak", "tracking_number": "TRK-3"},
                    {"id": "ship-1pak", "tracking_number": "TRK-1"},
                ]
                result = _run_inpost(draft, _SENDER, on_shipment_created=persisted.append)

        assert [shipment["package_type"] for shipment in result["courier_shipments"]] == [
            "3-pak",
            "1-pak",
        ]
        assert [shipment["id"] for shipment in persisted] == ["ship-3pak", "ship-1pak"]
        assert [call.kwargs["reference"] for call in mock_ship.call_args_list] == [
            "1050 | plastik | 3-pak",
            "1050 | plastik | 1-pak",
        ]

    @pytest.mark.parametrize(
        ("package_type", "material", "size"),
        [
            ("pół-pak", "plastik", "pół-pak"),
            # Glass carries its material in the type name; the reference must
            # not repeat it as "szkło | szkło".
            ("szkło", "szkło", "1-pak"),
            ("szkło-2pak", "szkło", "2-pak"),
        ],
    )
    def test_reference_identifies_package_material(self, package_type, material, size):
        from zdrovena.api.routers.webhooks import _run_inpost

        draft = {
            **_KURIER_DRAFT,
            "packages_breakdown": [{"type": package_type, "qty": 1}],
        }
        with patch("zdrovena.api.routers.webhooks.get_secret", return_value="tok"):
            with patch("zdrovena.common.inpost.InPostClient.create_kurier_shipment") as mock_ship:
                mock_ship.return_value = {"id": "ship-1", "tracking_number": "TRK-1"}
                _run_inpost(draft, _SENDER)

        assert mock_ship.call_args.kwargs["reference"] == f"1050 | {material} | {size}"


class TestInPostPayloadPlan:
    def test_plan_lists_one_payload_per_parcel(self, store):
        draft = {
            "id": "d1",
            "shopify_order_number": "1700",
            "courier": "inpost",
            "service": "inpost_courier_standard",
            "receiver": {
                "first_name": "Jan",
                "last_name": "Kowalski",
                "email": "j@example.com",
                "phone": "600200300",
            },
            "shipping_address": {
                "street": "Kwiatowa",
                "building_number": "5",
                "city": "Warszawa",
                "post_code": "00-001",
            },
            "packages": [{"type": "1-pak", "count": 1}],
        }
        sender = {"name": "Zdrovena", "street": "Cieszynska"}
        plan = _provider_inpost_payload_plan(draft, sender)

        assert len(plan) >= 1
        assert plan[0]["service"] == "inpost_courier_standard"
        assert "payload" in plan[0]

    def test_fixed_kurier_preview_payload_matches_golden(self):
        draft = {
            **_KURIER_DRAFT,
            "packages_breakdown": [{"type": "2-pak", "qty": 1}],
        }

        assert _provider_inpost_payload_plan(draft, _SENDER) == [
            {
                "service": "inpost_courier_standard",
                "package_type": "2-pak",
                "package_number": 1,
                "reference": "1050 | plastik | 2-pak",
                "payload": {
                    "service": "inpost_courier_standard",
                    "reference": "1050 | plastik | 2-pak",
                    "receiver": {
                        "first_name": "Jan",
                        "last_name": "Kowalski",
                        "email": "jan@k.pl",
                        "phone": "600100200",
                        "address": {
                            "street": "Kwiatowa 1",
                            "building_number": "1",
                            "city": "Warszawa",
                            "post_code": "00-001",
                            "country_code": "PL",
                        },
                    },
                    "sender": {
                        "company_name": "Zdrovena",
                        "first_name": "Zdrovena",
                        "last_name": "Zdrovena",
                        "email": "sender@zdrovena.pl",
                        "phone": "500000000",
                        "address": {
                            "street": "Testowa 1",
                            "building_number": "1",
                            "city": "Warszawa",
                            "post_code": "00-001",
                            "country_code": "PL",
                        },
                    },
                    "parcels": [
                        {
                            "dimensions": {
                                "unit": "mm",
                                "length": 400,
                                "width": 300,
                                "height": 200,
                            },
                            "weight": {"unit": "kg", "amount": 12.0},
                        }
                    ],
                    "custom_attributes": {"sending_method": "dispatch_order"},
                },
            }
        ]

    def test_plan_expands_a_multi_box_breakdown(self):
        draft = dict(_KURIER_DRAFT)
        draft["packages_breakdown"] = [{"type": "3-pak", "qty": 2}, {"type": "szkło", "qty": 1}]
        plan = _provider_inpost_payload_plan(draft, _SENDER)

        assert [entry["package_type"] for entry in plan] == ["3-pak", "3-pak", "szkło"]
        assert [entry["package_number"] for entry in plan] == [1, 2, 1]

    def test_preview_payload_is_what_the_execution_path_sends(self):
        """The preview is worthless if it can differ from the real request.

        Assert at the ShipX transport boundary, not at the client wrapper: the
        outage that started this work was a payload whose shape the carrier
        rejected, and every test that mocked the wrapper missed it.
        """
        from zdrovena.api.routers import webhooks as wh

        for draft in (_KURIER_DRAFT, _PACZKOMAT_DRAFT):
            plan = _provider_inpost_payload_plan(draft, _SENDER)
            with patch("zdrovena.api.routers.webhooks.get_secret", return_value="tok"):
                with patch(
                    "zdrovena.common.inpost.InPostClient._post_shipment",
                    return_value={"id": "s-1", "tracking_number": "T1"},
                ) as mock_post:
                    wh._run_inpost(draft, _SENDER)

            sent = [call.args[0] for call in mock_post.call_args_list]
            assert sent == [entry["payload"] for entry in plan]

    def test_partial_retry_previews_only_payloads_execution_will_send(self):
        from zdrovena.api.routers import webhooks as wh

        draft = {
            **_KURIER_DRAFT,
            "packages_breakdown": [{"type": "1-pak", "qty": 2}],
            "courier_shipments": [
                {
                    "id": "already-created",
                    "tracking_number": "TRK-1",
                    "package_type": "1-pak",
                    "package_number": "1",
                }
            ],
        }
        plan = _provider_inpost_payload_plan(draft, _SENDER)

        assert [(item["package_type"], item["package_number"]) for item in plan] == [("1-pak", 2)]
        with patch("zdrovena.api.routers.webhooks.get_secret", return_value="tok"):
            with patch(
                "zdrovena.common.inpost.InPostClient._post_shipment",
                return_value={"id": "new-shipment", "tracking_number": "TRK-2"},
            ) as mock_post:
                wh._run_inpost(draft, _SENDER)

        assert [call.args[0] for call in mock_post.call_args_list] == [plan[0]["payload"]]


class TestRunApaczka:
    def test_creates_shipment_returns_patch(self):
        from zdrovena.api.routers.webhooks import _run_apaczka

        storage_mock = object()
        draft = {
            "id": "d-ap",
            "shopify_order_number": "1060",
            "courier": "apaczka",
            "service": "apaczka",
            "apaczka_service_id": "53",
            "order_items": [{"name": "HUMIO 500 ml", "quantity": 2}],
            "receiver": {
                "first_name": "Piotr",
                "last_name": "W",
                "email": "p@w.pl",
                "phone": "800300400",
                "locker_id": "",
            },
            "shipping_address": {"street": "Wiśniowa 5", "city": "Gdańsk", "post_code": "80-001"},
        }
        with patch("zdrovena.api.routers.webhooks.get_secret", return_value="tok"):
            with patch("zdrovena.common.apaczka.ApaczkaClient.create_shipment") as mock_ship:
                mock_ship.return_value = {"id": "ap-1", "waybill_number": "WAY001"}
                result = _run_apaczka(draft, _SENDER, storage_mock)
        assert result["courier_draft_id"] == "ap-1"
        assert result["tracking_number"] == "WAY001"
        assert result["status"] == "created"
        assert mock_ship.call_args.kwargs["content"] == "2 x HUMIO 500 ml"
        assert mock_ship.call_args.kwargs["reference"] == "1060 | plastik | 1-pak"

    def test_passes_pickup_point_to_apaczka(self):
        from zdrovena.api.routers.webhooks import _run_apaczka

        draft = {
            "id": "d-ap-point",
            "shopify_order_number": "1648",
            "courier": "apaczka",
            "service": "apaczka",
            "apaczka_service_id": "23",
            "pickup_point": {
                "provider": "dpd",
                "id": "PL55338",
                "name": 'DPD Pickup- "Stokrotka Express"',
            },
            "receiver": {
                "first_name": "Adam",
                "last_name": "K",
                "email": "a@example.com",
                "phone": "+48500100200",
                "locker_id": "",
            },
            "shipping_address": {
                "street": "Puławska",
                "building_number": "5",
                "city": "Lublin",
                "post_code": "20-046",
            },
        }
        with patch("zdrovena.api.routers.webhooks.get_secret", return_value="tok"):
            with patch("zdrovena.common.apaczka.ApaczkaClient.create_shipment") as mock_ship:
                mock_ship.return_value = {"id": "ap-point", "waybill_number": "WAY-POINT"}
                _run_apaczka(draft, _SENDER, object())

        assert mock_ship.call_args.kwargs["receiver_point_id"] == "PL55338"

    def test_point_service_without_pickup_point_is_rejected(self):
        from zdrovena.api.routers.webhooks import _run_apaczka
        from zdrovena.common.shipping_exceptions import ApaczkaBusinessError

        draft = {
            "id": "d-ap-no-point",
            "shopify_order_number": "1648",
            "courier": "apaczka",
            "service": "apaczka",
            "apaczka_service_id": "23",
            "pickup_point": None,
            "receiver": {
                "first_name": "Adam",
                "last_name": "K",
                "email": "a@example.com",
                "phone": "+48500100200",
                "locker_id": "",
            },
            "shipping_address": {
                "street": "Puławska",
                "building_number": "5",
                "city": "Lublin",
                "post_code": "20-046",
            },
        }
        with patch("zdrovena.api.routers.webhooks.get_secret", return_value="tok"):
            with patch("zdrovena.common.apaczka.ApaczkaClient") as mock_client:
                with pytest.raises(ApaczkaBusinessError, match="no pickup point id"):
                    _run_apaczka(draft, _SENDER, object())

        mock_client.assert_not_called()

    def test_building_number_included_in_receiver_address(self):
        """Regression: shipping_address stores street and building_number separately
        (parse_pl_address splits Shopify address1). _run_apaczka must join them or
        Apaczka returns 500 with empty body when given a bare street name."""
        from zdrovena.api.routers.webhooks import _run_apaczka

        storage_mock = object()
        draft = {
            "id": "d-ap-bnum",
            "shopify_order_number": "1556",
            "courier": "apaczka",
            "service": "apaczka",
            "apaczka_service_id": "21",
            "receiver": {
                "first_name": "Piotr",
                "last_name": "G",
                "email": "p@g.pl",
                "phone": "500600700",
                "locker_id": "",
            },
            "shipping_address": {
                "street": "Krakowska",
                "building_number": "24",
                "city": "Nowy Sącz",
                "post_code": "33-300",
            },
        }
        with patch("zdrovena.api.routers.webhooks.get_secret", return_value="tok"):
            with patch("zdrovena.common.apaczka.ApaczkaClient.create_shipment") as mock_ship:
                mock_ship.return_value = {"id": "ap-bnum", "waybill_number": "WAY-BNUM"}
                _run_apaczka(draft, _SENDER, storage_mock)

        _, kwargs = mock_ship.call_args
        assert kwargs["receiver_address"] == "Krakowska 24"

    def test_flat_number_included_in_receiver_address(self):
        """flat_number (Shopify address2) must be appended so apartment is not lost."""
        from zdrovena.api.routers.webhooks import _run_apaczka

        storage_mock = object()
        draft = {
            "id": "d-ap-flat",
            "shopify_order_number": "1557",
            "courier": "apaczka",
            "service": "apaczka",
            "apaczka_service_id": "21",
            "receiver": {
                "first_name": "Anna",
                "last_name": "N",
                "email": "a@n.pl",
                "phone": "500600701",
                "locker_id": "",
            },
            "shipping_address": {
                "street": "Krakowska",
                "building_number": "24",
                "flat_number": "m. 5",
                "city": "Nowy Sącz",
                "post_code": "33-300",
            },
        }
        with patch("zdrovena.api.routers.webhooks.get_secret", return_value="tok"):
            with patch("zdrovena.common.apaczka.ApaczkaClient.create_shipment") as mock_ship:
                mock_ship.return_value = {"id": "ap-flat", "waybill_number": "WAY-FLAT"}
                _run_apaczka(draft, _SENDER, storage_mock)

        _, kwargs = mock_ship.call_args
        assert kwargs["receiver_address"] == "Krakowska 24 m. 5"

    def test_uses_draft_apaczka_service_id_not_secret(self):
        """P0 regression guard: service_id must come from the draft, never
        from a get_secret('apaczka_service_id') call (that secret no longer
        exists — see docs/superpowers/specs/2026-07-09-apaczka-per-draft-service.md)."""
        from zdrovena.api.routers.webhooks import _run_apaczka

        storage_mock = object()
        draft = {
            "id": "d-ap-2",
            "shopify_order_number": "1061",
            "courier": "apaczka",
            "service": "apaczka",
            "apaczka_service_id": "53",
            "receiver": {
                "first_name": "Anna",
                "last_name": "N",
                "email": "a@n.pl",
                "phone": "800300401",
                "locker_id": "",
            },
            "shipping_address": {"street": "Polna 1", "city": "Poznań", "post_code": "60-001"},
        }
        with patch("zdrovena.api.routers.webhooks.get_secret") as mock_get_secret:
            mock_get_secret.return_value = "tok"
            with patch("zdrovena.common.apaczka.ApaczkaClient") as MockClient:
                MockClient.return_value.create_shipment.return_value = {
                    "id": "ap-2",
                    "waybill_number": "WAY002",
                }
                _run_apaczka(draft, _SENDER, storage_mock)

        MockClient.assert_called_once_with("tok", "tok", "53", storage_mock)
        requested_secrets = [c.args[0] for c in mock_get_secret.call_args_list]
        assert "apaczka_service_id" not in requested_secrets

    def test_missing_apaczka_service_id_raises_instead_of_calling_client(self):
        """Critical safety guard: a draft with no apaczka_service_id (never matched
        against the Shopify shipping-line title map — see _pick_apaczka_service)
        must raise loudly rather than silently sending an empty service_id to
        Apaczka's live, paid create_shipment API."""
        from zdrovena.api.routers.webhooks import _run_apaczka
        from zdrovena.common.shipping_exceptions import ApaczkaBusinessError

        storage_mock = object()
        draft = {
            "id": "d-ap-3",
            "shopify_order_number": "1062",
            "courier": "apaczka",
            "service": "apaczka",
            "apaczka_service_id": None,
            "receiver": {
                "first_name": "Jan",
                "last_name": "K",
                "email": "j@k.pl",
                "phone": "800300402",
                "locker_id": "",
            },
            "shipping_address": {"street": "Krótka 2", "city": "Łódź", "post_code": "90-001"},
        }
        with patch("zdrovena.api.routers.webhooks.get_secret", return_value="tok"):
            with patch("zdrovena.common.apaczka.ApaczkaClient") as MockClient:
                with pytest.raises(ApaczkaBusinessError):
                    _run_apaczka(draft, _SENDER, storage_mock)

        MockClient.assert_not_called()


class TestListApaczkaServices:
    def test_returns_full_catalog(self, client):
        from zdrovena.common.apaczka import APACZKA_SERVICE_CATALOG

        resp = client.get("/api/shipping/apaczka-services")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["services"]) == len(APACZKA_SERVICE_CATALOG)
        assert {"service_id": "21", "label": "DPD Kurier"} in body["services"]


class TestCreateDraft:
    def test_inpost_kurier_draft_stored_on_success(self, store, tmp_path):

        storage = object()
        order = _load_fixture("shopify_order_inpost_kurier.json")
        _create_draft_for_test(order, store, storage)
        drafts = store.list_drafts()
        assert len(drafts) == 1
        d = drafts[0]
        assert d["courier"] == "inpost"
        assert d["service"] == "inpost_courier_standard"
        assert d["status"] == "pending"
        assert d["source"] == "shopify"
        assert d["packages_count"] == 1  # 2 zgrzewki szkła → 1×szkło-2pak
        assert d["packages_breakdown"] == [{"type": "szkło-2pak", "qty": 1}]
        assert d["tracking_number"] is None
        assert d["courier_draft_id"] is None
        assert d["shopify_order_number"] == "1002"
        assert d["receiver"]["first_name"] == "Piotr"
        assert d["receiver"]["last_name"] == "Nowak"
        assert d["receiver"]["email"] == "piotr.nowak@example.com"
        assert d["shipping_address"]["city"] == "Kraków"
        assert d["shipping_address"]["post_code"] == "30-001"
        assert d["shipping_address"]["flat_number"] == "m. 5"

    def test_locker_id_from_address2_fallback(self, store):

        storage = object()
        order = {
            "id": "101",
            "order_number": 2002,
            "shipping_lines": [{"title": "InPost Paczkomat"}],
            "line_items": [{"quantity": 1}],
            "shipping_address": {
                "first_name": "Anna",
                "last_name": "N",
                "address1": "Różana 3",
                "address2": "WAW01A",
                "city": "Kraków",
                "zip": "31-001",
                "phone": "",
            },
            "customer": {},
            "email": "",
            "note_attributes": [],
        }
        _create_draft_for_test(order, store, storage)
        drafts = store.list_drafts()
        assert len(drafts) == 1
        d = drafts[0]
        assert d["status"] == "pending"
        assert d["service"] == "inpost_locker_standard"
        assert d["receiver"]["locker_id"] == "WAW01A"

    def test_multipackage_inpost_with_phone_is_pending(self, store):

        order = {
            "id": "800",
            "order_number": 9002,
            "shipping_lines": [{"title": "InPost Kurier"}],
            "line_items": [
                {"name": "HUMIO - woda alkaliczna, 12 butelek", "quantity": 5},
            ],
            "shipping_address": {
                "first_name": "Jan",
                "last_name": "K",
                "address1": "Testowa 5",
                "address2": "",
                "city": "Wrocław",
                "zip": "50-001",
                "phone": "600100200",
            },
            "customer": {},
            "email": "jan@k.pl",
            "note_attributes": [],
        }
        _create_draft_for_test(order, store, object())
        d = store.list_drafts()[0]
        assert d["packages_count"] == 2
        assert d["status"] == "pending"


class TestCreateDraftAllegroDelivery:
    """Routing na 'allegro_delivery' (Wysyłam z Allegro) dla source='allegro'
    z AllegroDeliveryMethodId — zastępuje InPost/Apaczkę całkowicie."""

    def _base_allegro_order(self, title: str, method_id: str, pickup_id=None):
        note_attrs = []
        if method_id:
            note_attrs.append({"name": "AllegroDeliveryMethodId", "value": method_id})
        if pickup_id:
            note_attrs.append({"name": "PickupPointId", "value": pickup_id})
        return {
            "id": "AL-9001",
            "order_number": 9001,
            "shipping_lines": [{"title": title}],
            "line_items": [{"quantity": 1, "name": "Woda 500ml"}],
            "shipping_address": {
                "first_name": "Jan",
                "last_name": "Kowalski",
                "address1": "Marszałkowska 1",
                "address2": "",
                "city": "Warszawa",
                "zip": "00-001",
                "phone": "+48123456789",
            },
            "customer": {},
            "email": "jan@example.com",
            "note_attributes": note_attrs,
        }

    def test_allegro_paczkomat_routes_to_allegro_delivery(self, store):

        storage = object()
        order = self._base_allegro_order(
            title="InPost Paczkomat (WAW10A)",
            method_id="c50d09e8-3b32-4e7a-8f4c-11a2b3c4d5e6",
            pickup_id="WAW10A",
        )
        _create_draft_for_test(order, store, storage, source="allegro")

        d = store.list_drafts()[0]
        assert d["source"] == "allegro"
        assert d["courier"] == "allegro_delivery"
        assert d["service"] == "allegro_delivery"
        assert d["allegro_delivery_method_id"] == "c50d09e8-3b32-4e7a-8f4c-11a2b3c4d5e6"
        assert d["allegro_credentials_id"] is None
        assert d["allegro_sending_method"] == "parcel_locker"

    def test_allegro_inpost_kurier_no_default_sending_method(self, store, monkeypatch):
        """Po ostatnich problemach z InPost sandbox — InPost Kurier NIE ma domyślnego
        sending_method. Operator musi świadomie ustawić albo włączyć flagę env."""
        monkeypatch.delenv("ALLEGRO_INPOST_KURIER_DEFAULT", raising=False)

        storage = object()
        order = self._base_allegro_order(
            title="InPost Kurier", method_id="aa11bb22-cc33-dd44-ee55-ff6677889900"
        )
        _create_draft_for_test(order, store, storage, source="allegro")

        d = store.list_drafts()[0]
        assert d["courier"] == "allegro_delivery"
        assert d["allegro_sending_method"] is None

    def test_allegro_inpost_kurier_env_flag_sets_dispatch_order(self, store, monkeypatch):
        """Za flagą operatora — można włączyć domyślny sending_method dla InPost Kurier."""
        monkeypatch.setenv("ALLEGRO_INPOST_KURIER_DEFAULT", "dispatch_order")

        storage = object()
        order = self._base_allegro_order(
            title="InPost Kurier", method_id="aa11bb22-cc33-dd44-ee55-ff6677889900"
        )
        _create_draft_for_test(order, store, storage, source="allegro")

        d = store.list_drafts()[0]
        assert d["allegro_sending_method"] == "dispatch_order"

    def test_allegro_non_inpost_no_sending_method(self, store):

        storage = object()
        order = self._base_allegro_order(
            title="Kurier DPD", method_id="11111111-2222-3333-4444-555555555555"
        )
        _create_draft_for_test(order, store, storage, source="allegro")

        d = store.list_drafts()[0]
        assert d["courier"] == "allegro_delivery"
        assert d["allegro_sending_method"] is None

    def test_allegro_without_method_id_fallback_to_apaczka(self, store):

        storage = object()
        order = self._base_allegro_order(title="Kurier DPD", method_id="")
        _create_draft_for_test(order, store, storage, source="allegro")

        d = store.list_drafts()[0]
        assert d["courier"] == "apaczka"
        assert d["service"] == "apaczka"

    def test_shopify_source_ignores_allegro_method_id(self, store):

        storage = object()
        order = self._base_allegro_order(
            title="InPost Paczkomat (WAW10A)", method_id="c50d09e8-3b32"
        )
        _create_draft_for_test(order, store, storage, source="shopify")

        d = store.list_drafts()[0]
        assert d["source"] == "shopify"
        assert d["courier"] == "inpost"


# ── Label endpoint ────────────────────────────────────────────────────────────


class TestGetLabel:
    def _seed_created_draft(self, store, courier="inpost"):
        service = "inpost_courier_standard" if courier == "inpost" else "apaczka"
        draft = {
            "id": "label-draft-1",
            "created_at": "2026-05-20T10:00:00+00:00",
            "source": "shopify",
            "shopify_order_id": "50",
            "shopify_order_number": "5000",
            "customer_name": "Test",
            "courier": courier,
            "service": service,
            "tracking_number": "TRK",
            "courier_draft_id": "courier-id-1",
            "status": "created",
            "packages_count": 1,
            "pickup_ordered": False,
            "receiver": {
                "first_name": "T",
                "last_name": "T",
                "email": "",
                "phone": "",
                "locker_id": "",
            },
            "shipping_address": {"street": "X", "city": "Y", "post_code": "Z"},
            "parcel": {"template": "small", "weight_kg": None},
            "error": None,
        }
        store.upsert_draft(draft)
        return draft

    def test_404_when_draft_not_found(self, client):
        resp = client.get("/api/shipping/drafts/nonexistent/label?courier=inpost")
        assert resp.status_code == 404

    def test_404_when_no_courier_draft_id(self, client, store):
        draft = self._seed_created_draft(store)
        store.update_draft(draft["id"], {"courier_draft_id": None})
        resp = client.get(f"/api/shipping/drafts/{draft['id']}/label?courier=inpost")
        assert resp.status_code == 404

    def test_400_for_unknown_courier(self, client, store):
        # Draft with no courier field + invalid query param → 400
        draft = self._seed_created_draft(store)
        store.update_draft(draft["id"], {"courier": ""})
        resp = client.get(f"/api/shipping/drafts/{draft['id']}/label?courier=unknown")
        assert resp.status_code == 400

    def test_inpost_label_returns_pdf(self, client, store):
        draft = self._seed_created_draft(store, courier="inpost")
        with patch("zdrovena.api.routers.webhooks.get_secret", return_value="tok"):
            with patch(
                "zdrovena.common.inpost.InPostClient.get_label", return_value=b"%PDF-1.4 fake"
            ):
                resp = client.get(f"/api/shipping/drafts/{draft['id']}/label?courier=inpost")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"

    def test_inpost_multiple_box_labels_are_merged(self, client, store):
        draft = self._seed_created_draft(store, courier="inpost")
        store.update_draft(
            draft["id"],
            {
                "courier_shipments": [
                    {"id": "ship-3pak", "package_type": "3-pak", "package_number": "1"},
                    {"id": "ship-1pak", "package_type": "1-pak", "package_number": "1"},
                ]
            },
        )
        with patch("zdrovena.api.routers.webhooks.get_secret", return_value="tok"):
            with patch(
                "zdrovena.common.inpost.InPostClient.get_label",
                side_effect=[b"first", b"second"],
            ) as mock_label:
                with patch(
                    "zdrovena.api.routers.webhooks._merge_pdfs", return_value=b"merged"
                ) as merge:
                    resp = client.get(f"/api/shipping/drafts/{draft['id']}/label?courier=inpost")

        assert resp.status_code == 200
        assert resp.content == b"merged"
        assert [call.args[0] for call in mock_label.call_args_list] == ["ship-3pak", "ship-1pak"]
        merge.assert_called_once_with([b"first", b"second"])

    def test_label_502_on_courier_error(self, client, store):
        draft = self._seed_created_draft(store, courier="inpost")
        with patch("zdrovena.api.routers.webhooks.get_secret", return_value="tok"):
            with patch(
                "zdrovena.common.inpost.InPostClient.get_label",
                side_effect=Exception("courier down"),
            ):
                resp = client.get(f"/api/shipping/drafts/{draft['id']}/label?courier=inpost")
        assert resp.status_code == 502


class TestGetLabelAllegroDelivery:
    def _seed(self, store, **overrides):
        draft = {
            "id": "draft-lbl-allegro-1",
            "created_at": "2026-05-20T10:00:00+00:00",
            "source": "allegro",
            "shopify_order_number": "5500",
            "customer_name": "Test",
            "courier": "allegro_delivery",
            "status": "created",
            "allegro_shipment_id": "ship-lbl-777",
            "courier_draft_id": "ship-lbl-777",
        }
        draft.update(overrides)
        store.upsert_draft(draft)
        return draft

    def test_allegro_delivery_label_returns_pdf(self, client, store):
        draft = self._seed(store)
        allegro = MagicMock()
        allegro.get_ship_with_allegro_label.return_value = b"%PDF-1.4 allegro"
        with patch("zdrovena.api.routers.webhooks._get_allegro_client", return_value=allegro):
            resp = client.get(f"/api/shipping/drafts/{draft['id']}/label")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.content == b"%PDF-1.4 allegro"
        allegro.get_ship_with_allegro_label.assert_called_once_with("ship-lbl-777")

    def test_allegro_delivery_falls_back_to_courier_draft_id(self, client, store):
        draft = self._seed(store, allegro_shipment_id=None, courier_draft_id="fallback-id-9")
        allegro = MagicMock()
        allegro.get_ship_with_allegro_label.return_value = b"%PDF-fallback"
        with patch("zdrovena.api.routers.webhooks._get_allegro_client", return_value=allegro):
            resp = client.get(f"/api/shipping/drafts/{draft['id']}/label")
        assert resp.status_code == 200
        allegro.get_ship_with_allegro_label.assert_called_once_with("fallback-id-9")

    def test_allegro_delivery_502_on_business_error(self, client, store):
        from zdrovena.common.shipping_exceptions import AllegroBusinessError

        draft = self._seed(store)
        allegro = MagicMock()
        allegro.get_ship_with_allegro_label.side_effect = AllegroBusinessError(
            detail="not ready", action="label"
        )
        with patch("zdrovena.api.routers.webhooks._get_allegro_client", return_value=allegro):
            resp = client.get(f"/api/shipping/drafts/{draft['id']}/label")
        assert resp.status_code == 502

    def test_allegro_delivery_502_when_client_missing(self, client, store):
        draft = self._seed(store)
        with patch("zdrovena.api.routers.webhooks._get_allegro_client", return_value=None):
            resp = client.get(f"/api/shipping/drafts/{draft['id']}/label")
        assert resp.status_code == 502

    def test_allegro_delivery_404_when_no_shipment_id(self, client, store):
        draft = self._seed(store, allegro_shipment_id=None, courier_draft_id=None)
        resp = client.get(f"/api/shipping/drafts/{draft['id']}/label")
        assert resp.status_code == 404


# ── Additional coverage tests ─────────────────────────────────────────────────


class TestCreateDraftPaczkomat:
    def test_paczkomat_draft_stored(self, store):

        storage = object()
        order = _load_fixture("shopify_order_inpost_paczkomat.json")
        _create_draft_for_test(order, store, storage)
        drafts = store.list_drafts()
        assert len(drafts) == 1
        d = drafts[0]
        assert d["service"] == "inpost_locker_standard"
        assert d["status"] == "pending"
        assert d["shopify_order_number"] == "1001"
        assert d["receiver"]["first_name"] == "Anna"
        assert d["receiver"]["last_name"] == "Kowalska"
        assert d["receiver"]["locker_id"] == "WAW123A"
        assert d["pickup_point"]["provider"] == "inpost"
        assert d["pickup_point"]["id"] == "WAW123A"
        assert d["shipping_address"]["city"] == "Warszawa"
        assert d["tracking_number"] is None
        assert d["courier_draft_id"] is None


class TestCreateDraftApaczka:
    def test_apaczka_draft_stored(self, store):

        storage = object()
        order = _load_fixture("shopify_order_apaczka.json")
        _create_draft_for_test(order, store, storage)
        drafts = store.list_drafts()
        assert len(drafts) == 1
        d = drafts[0]
        assert d["courier"] == "apaczka"
        assert d["service"] == "apaczka"
        assert d["status"] == "needs_review"  # phone is null in fixture, so needs_review
        assert d["tracking_number"] is None
        assert d["courier_draft_id"] is None
        assert d["shopify_order_number"] == "1003"
        assert d["receiver"]["first_name"] == "Maria"
        assert d["receiver"]["last_name"] == "Wiśniewska"
        assert d["receiver"]["email"] == "maria.wisniewska@example.com"
        assert d["shipping_address"]["city"] == "Gdańsk"

    def test_octolize_dpd_pickup_is_automatic(self, store):

        order = _load_fixture("shopify_order_dpd_pickup.json")
        order["order_number"] = 1648
        order["fulfillment_status"] = None
        order["fulfillments"] = []
        order["shipping_address"]["phone"] = "534644600"
        order["shipping_lines"][0].update(
            {
                "code": "pickup-points:8830:147893:171",
                "title": 'DPD • DPD Pickup- "Stokrotka Express" • 0.17 km • PL55338',
            }
        )
        for attr in order["note_attributes"]:
            replacements = {
                "PickupPointId": "PL55338",
                "PickupPointName": 'DPD Pickup- "Stokrotka Express"',
                "PickupPointAddress": "Puławska 5",
                "PickupPointPostCode": "20046",
                "PickupPointCity": "Lublin",
            }
            if attr["name"] in replacements:
                attr["value"] = replacements[attr["name"]]

        _create_draft_for_test(order, store, object())

        draft = store.list_drafts()[0]
        assert draft["shopify_order_number"] == "1648"
        assert draft["courier"] == "apaczka"
        assert draft["apaczka_service_id"] == "23"
        assert draft["pickup_point"]["provider"] == "dpd"
        assert draft["pickup_point"]["id"] == "PL55338"
        assert draft["shipping_service_match_status"] == "auto_matched"
        assert draft["status"] == "pending"

    def test_octolize_poczta_pickup_is_automatic(self, store):

        order = _load_fixture("shopify_order_dpd_pickup.json")
        order["order_number"] = 1642
        order["fulfillment_status"] = None
        order["fulfillments"] = []
        order["shipping_address"]["phone"] = "500600700"
        order["shipping_lines"][0].update(
            {
                "code": "pickup-points:8828:1655717:352",
                "title": "Poczta Polska • Sklep Żabka • 0.35 km • 318409",
            }
        )
        pickup_values = {
            "PickupPointCourier": "Poczta Polska",
            "PickupPointId": "318409",
            "PickupPointName": "Sklep Żabka",
            "PickupPointAddress": "Stefana Żeromskiego 52",
            "PickupPointPostCode": "26-110",
            "PickupPointCity": "Skarżysko-Kamienna",
        }
        for attr in order["note_attributes"]:
            if attr["name"] in pickup_values:
                attr["value"] = pickup_values[attr["name"]]

        _create_draft_for_test(order, store, object())

        draft = store.list_drafts()[0]
        assert draft["courier"] == "apaczka"
        assert draft["apaczka_service_id"] == "64"
        assert draft["pickup_point"]["provider"] == "poczta"
        assert draft["pickup_point"]["id"] == "318409"
        assert draft["status"] == "pending"

    def test_known_pickup_provider_without_point_id_stays_in_review(self, store):

        order = _load_fixture("shopify_order_dpd_pickup.json")
        order["fulfillment_status"] = None
        order["fulfillments"] = []
        order["shipping_address"]["phone"] = "500600700"
        order["shipping_lines"][0]["title"] = "DPD • DPD Pickup"
        for attr in order["note_attributes"]:
            if attr["name"] == "PickupPointId":
                attr["value"] = ""

        _create_draft_for_test(order, store, object())

        draft = store.list_drafts()[0]
        assert draft["apaczka_service_id"] is None
        assert draft["pickup_point"]["provider"] == "dpd"
        assert draft["pickup_point"]["id"] == ""
        assert draft["status"] == "needs_review"
        assert draft["shipping_service_match_detail"] == (
            "Shopify pickup point is missing PickupPointId"
        )

    def test_unknown_pickup_provider_is_not_guessed(self, store):

        order = _load_fixture("shopify_order_dpd_pickup.json")
        order["fulfillment_status"] = None
        order["fulfillments"] = []
        order["shipping_address"]["phone"] = "500600700"
        order["shipping_lines"][0].update(
            {
                "code": "pickup-points:9999:123:100",
                "title": "Nieznany operator • Punkt • 0.1 km • XYZ123",
            }
        )
        for attr in order["note_attributes"]:
            if attr["name"] == "PickupPointCourier":
                attr["value"] = "Nieznany operator"
            if attr["name"] == "PickupPointId":
                attr["value"] = "XYZ123"

        _create_draft_for_test(order, store, object())

        draft = store.list_drafts()[0]
        assert draft["courier"] == "apaczka"
        assert draft["apaczka_service_id"] is None
        assert draft["pickup_point"]["provider"] == ""
        assert draft["pickup_point"]["id"] == "XYZ123"
        assert draft["status"] == "needs_review"

    def test_shopify_sync_backfills_existing_unmatched_pickup_draft(self, store):
        from zdrovena.api.routers.webhooks import _build_draft_record

        order = _load_fixture("shopify_order_dpd_pickup.json")
        order["fulfillment_status"] = None
        order["fulfillments"] = []
        order["shipping_address"]["phone"] = "500600700"
        existing = _build_draft_record(order)
        existing.update(
            {
                "apaczka_service_id": None,
                "pickup_point": None,
                "shipping_service_match_status": "requires_selection",
                "shipping_service_match_detail": "Legacy unmatched draft",
                "status": "needs_review",
            }
        )
        store.upsert_draft(existing)

        changed = _sync_draft_from_order_for_test(order, store, object(), existing=existing)

        updated = store.get_draft(existing["id"])
        assert changed is True
        assert updated is not None
        assert updated["apaczka_service_id"] == "23"
        assert updated["pickup_point"]["id"] == "PL72095"
        assert updated["status"] == "pending"

    def test_apaczka_service_id_set_from_title_map(self, store, monkeypatch):
        from zdrovena.api.routers.webhooks import _reset_courier_maps_cache

        monkeypatch.setenv("APACZKA_SERVICE_TITLE_MAP", "dpd=21")
        _reset_courier_maps_cache()
        try:
            storage = object()
            order = _load_fixture("shopify_order_apaczka.json")
            _create_draft_for_test(order, store, storage)
            drafts = store.list_drafts()
            assert drafts[0]["apaczka_service_id"] == "21"
            assert drafts[0]["shipping_service_match_status"] == "auto_matched"
            assert drafts[0]["shipping_service_match_source"] == "Apaczka DPD"
            assert "APACZKA_SERVICE_TITLE_MAP" in drafts[0]["shipping_service_match_detail"]
        finally:
            monkeypatch.delenv("APACZKA_SERVICE_TITLE_MAP", raising=False)
            _reset_courier_maps_cache()

    def test_apaczka_service_id_none_forces_needs_review(self, store, monkeypatch):
        """Fixture's shipping_lines[0].title is 'Apaczka DPD' — with no env
        mapping configured, apaczka_service_id stays unset and the draft must
        be needs_review even if phone/packages_count would otherwise pass."""
        from zdrovena.api.routers.webhooks import _reset_courier_maps_cache

        monkeypatch.delenv("APACZKA_SERVICE_TITLE_MAP", raising=False)
        _reset_courier_maps_cache()
        try:
            order = _load_fixture("shopify_order_apaczka.json")
            order["shipping_address"]["phone"] = "500600700"
            order["customer"]["phone"] = "500600700"
            storage = object()
            _create_draft_for_test(order, store, storage)
            drafts = store.list_drafts()
            assert drafts[0]["apaczka_service_id"] is None
            assert drafts[0]["shipping_service_match_status"] == "requires_selection"
            assert drafts[0]["shipping_service_match_source"] == "Apaczka DPD"
            assert drafts[0]["status"] == "needs_review"
        finally:
            monkeypatch.delenv("APACZKA_SERVICE_TITLE_MAP", raising=False)
            _reset_courier_maps_cache()

    def test_apaczka_service_id_matched_allows_pending(self, store, monkeypatch):
        """Same phone fix as above, but WITH a matching title map — status
        should be 'pending', proving apaczka_service_id was the only blocker."""
        from zdrovena.api.routers.webhooks import _reset_courier_maps_cache

        monkeypatch.setenv("APACZKA_SERVICE_TITLE_MAP", "dpd=21")
        _reset_courier_maps_cache()
        try:
            order = _load_fixture("shopify_order_apaczka.json")
            order["shipping_address"]["phone"] = "500600700"
            order["customer"]["phone"] = "500600700"
            storage = object()
            _create_draft_for_test(order, store, storage)
            drafts = store.list_drafts()
            assert drafts[0]["apaczka_service_id"] == "21"
            assert drafts[0]["status"] == "pending"
        finally:
            monkeypatch.delenv("APACZKA_SERVICE_TITLE_MAP", raising=False)
            _reset_courier_maps_cache()

    def test_apaczka_service_id_from_uncatalogued_title_map_forces_needs_review(
        self, store, monkeypatch
    ):
        """Regression guard for a real gap found in final-branch review: an
        operator misconfiguring APACZKA_SERVICE_TITLE_MAP with a service_id
        that isn't in APACZKA_SERVICE_CATALOG must NOT silently create a
        'pending' draft that would ship through an uncatalogued/wrong
        courier channel — it must fall back to needs_review, exactly like
        an unconfigured title map does."""
        from zdrovena.api.routers.webhooks import _reset_courier_maps_cache

        monkeypatch.setenv("APACZKA_SERVICE_TITLE_MAP", "dpd=999999")
        _reset_courier_maps_cache()
        try:
            order = _load_fixture("shopify_order_apaczka.json")
            order["shipping_address"]["phone"] = "500600700"
            order["customer"]["phone"] = "500600700"
            storage = object()
            _create_draft_for_test(order, store, storage)
            drafts = store.list_drafts()
            assert drafts[0]["apaczka_service_id"] is None
            assert drafts[0]["status"] == "needs_review"
        finally:
            monkeypatch.delenv("APACZKA_SERVICE_TITLE_MAP", raising=False)
            _reset_courier_maps_cache()

    def test_non_apaczka_draft_has_none_apaczka_service_id(self, store):
        """InPost/Allegro drafts get apaczka_service_id=None, never validated."""

        storage = object()
        order = _load_fixture("shopify_order_inpost_kurier.json")
        _create_draft_for_test(order, store, storage)
        drafts = store.list_drafts()
        assert drafts[0]["apaczka_service_id"] is None

    def test_manual_apaczka_service_match_survives_later_sync(self, store, monkeypatch):
        from zdrovena.api.routers.webhooks import (
            _reset_courier_maps_cache,
        )

        monkeypatch.delenv("APACZKA_SERVICE_TITLE_MAP", raising=False)
        _reset_courier_maps_cache()
        order = _load_fixture("shopify_order_apaczka.json")
        order["shipping_address"]["phone"] = "500600700"
        order["customer"]["phone"] = "500600700"
        try:
            _create_draft_for_test(order, store, object())
            draft = store.list_drafts()[0]
            store.update_draft(
                draft["id"],
                {
                    "apaczka_service_id": "53",
                    "shipping_service_match_status": "manual",
                    "shipping_service_match_source": "operator",
                    "shipping_service_match_detail": "Manual Apaczka service override",
                    "status": "pending",
                },
            )

            monkeypatch.setenv("APACZKA_SERVICE_TITLE_MAP", "dpd=21")
            _reset_courier_maps_cache()
            _sync_draft_from_order_for_test(
                order, store, object(), existing=store.get_draft(draft["id"])
            )

            updated = store.get_draft(draft["id"])
            assert updated["apaczka_service_id"] == "53"
            assert updated["shipping_service_match_status"] == "manual"
            assert updated["shipping_service_match_source"] == "operator"
            assert updated["shipping_service_match_detail"] == "Manual Apaczka service override"
        finally:
            monkeypatch.delenv("APACZKA_SERVICE_TITLE_MAP", raising=False)
            _reset_courier_maps_cache()


class TestExecuteDraftApaczka:
    def test_execute_apaczka_draft(self, client, store):
        draft = {
            "id": "draft-ap-exec",
            "created_at": "2026-05-20T10:00:00+00:00",
            "source": "shopify",
            "shopify_order_id": "40",
            "shopify_order_number": "1300",
            "customer_name": "Zofia K",
            "courier": "apaczka",
            "service": "apaczka",
            "tracking_number": None,
            "courier_draft_id": None,
            "status": "error",
            "packages_count": 1,
            "pickup_ordered": False,
            "receiver": {
                "first_name": "Zofia",
                "last_name": "K",
                "email": "z@k.pl",
                "phone": "900000000",
                "locker_id": "",
            },
            "shipping_address": {
                "street": "Modrzewska 2",
                "city": "Wrocław",
                "post_code": "50-001",
            },
            "parcel": {"template": "small", "weight_kg": None},
            "error": "creds missing",
        }
        store.upsert_draft(draft)
        with patch(
            "zdrovena.api.routers.webhooks._run_apaczka",
            return_value={
                "courier_draft_id": "ap-exec-1",
                "tracking_number": "WAY-X",
                "status": "created",
                "error": None,
            },
        ):
            resp = client.post(f"/api/shipping/drafts/{draft['id']}/execute")
        assert resp.status_code == 200
        updated = store.get_draft(draft["id"])
        assert updated["status"] == "created"


class TestOrderPickupEdgeCases:
    def _seed(self, store, **overrides):
        draft = {
            "id": "pickup-edge-1",
            "created_at": "2026-05-20T10:00:00+00:00",
            "source": "shopify",
            "shopify_order_id": "60",
            "shopify_order_number": "1400",
            "customer_name": "Test",
            "courier": "inpost",
            "service": "inpost_courier_standard",
            "tracking_number": "TRK",
            "courier_draft_id": "c-id-1",
            "status": "created",
            "packages_count": 1,
            "pickup_ordered": False,
            "receiver": {
                "first_name": "T",
                "last_name": "T",
                "email": "",
                "phone": "",
                "locker_id": "",
            },
            "shipping_address": {"street": "X", "city": "Y", "post_code": "Z"},
            "parcel": {"template": "small", "weight_kg": None},
            "error": None,
        }
        draft.update(overrides)
        store.upsert_draft(draft)
        return draft

    def test_409_when_no_courier_draft_id(self, client, store):
        draft = self._seed(store, courier_draft_id=None)
        resp = client.post(f"/api/shipping/drafts/{draft['id']}/pickup")
        assert resp.status_code == 409

    def test_502_on_dispatch_error(self, client, store):
        draft = self._seed(store)
        with patch("zdrovena.api.routers.webhooks.get_secret", return_value="tok"):
            with patch(
                "zdrovena.common.inpost.InPostClient.create_dispatch_order",
                side_effect=Exception("inpost down"),
            ):
                resp = client.post(f"/api/shipping/drafts/{draft['id']}/pickup")
        assert resp.status_code == 502


class TestGetLabelApaczka:
    def test_apaczka_label_returns_pdf(self, client, store):
        draft = {
            "id": "label-ap-1",
            "created_at": "2026-05-20T10:00:00+00:00",
            "source": "shopify",
            "shopify_order_id": "70",
            "shopify_order_number": "7000",
            "customer_name": "Test",
            "courier": "apaczka",
            "service": "apaczka",
            "tracking_number": "WAY",
            "courier_draft_id": "ap-draft-1",
            "status": "created",
            "packages_count": 1,
            "pickup_ordered": False,
            "receiver": {
                "first_name": "T",
                "last_name": "T",
                "email": "",
                "phone": "",
                "locker_id": "",
            },
            "shipping_address": {"street": "X", "city": "Y", "post_code": "Z"},
            "parcel": {"template": "small", "weight_kg": 1.0},
            "error": None,
        }
        store.upsert_draft(draft)
        with patch("zdrovena.api.routers.webhooks.get_secret") as mock_get_secret:
            mock_get_secret.return_value = "tok"
            with patch(
                "zdrovena.common.apaczka.ApaczkaClient.get_label", return_value=b"%PDF-1.4 apaczka"
            ):
                resp = client.get(f"/api/shipping/drafts/{draft['id']}/label?courier=apaczka")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        requested_secrets = [c.args[0] for c in mock_get_secret.call_args_list]
        assert "apaczka_service_id" not in requested_secrets


class TestCreateDraftDispatchFail:
    def test_kurier_draft_pending_no_courier_api_called(self, store):

        storage = object()
        order = {
            "id": "400",
            "order_number": 5001,
            "shipping_lines": [{"title": "InPost Kurier"}],
            "line_items": [{"quantity": 3}],
            "shipping_address": {
                "first_name": "Leon",
                "last_name": "M",
                "address1": "Brzozowa 8",
                "address2": "",
                "city": "Kraków",
                "zip": "31-100",
                "phone": "",
            },
            "customer": {},
            "email": "l@m.pl",
            "note_attributes": [],
        }
        with patch("zdrovena.common.inpost.InPostClient.create_kurier_shipment") as mock_api:
            _create_draft_for_test(order, store, storage)
            mock_api.assert_not_called()
        drafts = store.list_drafts()
        assert len(drafts) == 1
        d = drafts[0]
        assert d["status"] == "pending"
        assert d["packages_count"] == 1
        assert d["courier_draft_id"] is None


class TestCreateDraftKaucjaFilter:
    def test_kaucja_excluded_from_packages_and_order_items(self, store):

        storage = object()
        order = {
            "id": "500",
            "order_number": 6001,
            "shipping_lines": [{"title": "InPost Kurier"}],
            "line_items": [
                {"name": "HUMIO woda alkaliczna 6-pak", "quantity": 3},
                {"name": "Kaucja szklana butelka 6 szt.", "quantity": 3},
            ],
            "shipping_address": {
                "first_name": "Jan",
                "last_name": "K",
                "address1": "Lipowa 1",
                "address2": "",
                "city": "Warszawa",
                "zip": "00-001",
                "phone": "",
            },
            "customer": {},
            "email": "jan@k.pl",
            "note_attributes": [],
        }
        _create_draft_for_test(order, store, storage)
        drafts = store.list_drafts()
        assert len(drafts) == 1
        d = drafts[0]
        assert d["packages_count"] == 1
        item_names = [i["name"] for i in d["order_items"]]
        assert all("kaucja" not in n.lower() for n in item_names)
        assert len(d["order_items"]) == 1


class TestCalcPackages:
    """Unit tests for the typed parcel planning algorithm."""

    def _items(self, *specs):
        """Build product_items list from (name, qty) tuples."""
        return [{"name": n, "quantity": q} for n, q in specs]

    def _run(self, *specs):
        items = self._items(*specs)
        plan = calc_packages(items)
        count, breakdown = plan.to_legacy_tuple()

        assert isinstance(plan, PackagePlan)
        assert all(isinstance(item, PackageBreakdownItem) for item in plan.breakdown)
        assert count == plan.package_count
        assert breakdown == [item.to_legacy_dict() for item in plan.breakdown]

        bd = {b["type"]: b["qty"] for b in breakdown}
        return count, bd

    def test_typed_plan_has_exact_legacy_serialization(self):
        plan = calc_packages(self._items(("HUMIO - woda alkaliczna, 12 butelek", 5)))

        assert plan.package_count == 2
        assert plan.breakdown == (
            PackageBreakdownItem(package_type="3-pak", quantity=1),
            PackageBreakdownItem(package_type="2-pak", quantity=1),
        )
        assert plan.to_legacy_tuple() == (
            2,
            [
                {"type": "3-pak", "qty": 1},
                {"type": "2-pak", "qty": 1},
            ],
        )

    # ── Plastik ───────────────────────────────────────────────────────────────

    def test_plastik_3_zgrzewki_one_3pak(self):
        count, bd = self._run(("HUMIO - woda alkaliczna, 12 butelek", 3))
        assert count == 1
        assert bd == {"3-pak": 1}

    def test_plastik_6_zgrzewki_two_3pak(self):
        count, bd = self._run(("HUMIO - woda alkaliczna, 12 butelek", 6))
        assert count == 2
        assert bd == {"3-pak": 2}

    def test_plastik_5_zgrzewki_3pak_plus_2pak(self):
        count, bd = self._run(("HUMIO - woda alkaliczna, 12 butelek", 5))
        assert count == 2
        assert bd == {"3-pak": 1, "2-pak": 1}

    def test_plastik_4_zgrzewki_3pak_plus_1pak(self):
        count, bd = self._run(("HUMIO - woda alkaliczna, 12 butelek", 4))
        assert count == 2
        assert bd == {"3-pak": 1, "1-pak": 1}

    def test_plastik_2_zgrzewki_one_2pak(self):
        count, bd = self._run(("HUMIO - woda alkaliczna, 12 butelek", 2))
        assert count == 1
        assert bd == {"2-pak": 1}

    def test_plastik_1_zgrzewka_one_1pak(self):
        count, bd = self._run(("HUMIO - woda alkaliczna, 12 butelek", 1))
        assert count == 1
        assert bd == {"1-pak": 1}

    def test_plastik_6_butelek_one_half_pack_regression_1648(self):
        count, bd = self._run(("HUMIO - woda alkaliczna, 6 butelek", 1))
        assert count == 1
        assert bd == {"pół-pak": 1}

    def test_plastik_24_butelki_one_2pak(self):
        count, bd = self._run(("HUMIO - woda alkaliczna, 24 butelki", 1))
        assert count == 1
        assert bd == {"2-pak": 1}

    def test_plastik_7_zgrzewki_two_3pak_plus_1pak(self):
        count, bd = self._run(("HUMIO - woda alkaliczna, 12 butelek", 7))
        assert count == 3
        assert bd == {"3-pak": 2, "1-pak": 1}

    # ── Szkło ─────────────────────────────────────────────────────────────────

    def test_szklo_1_zgrzewka_one_box(self):
        count, bd = self._run(("HUMIO - woda alkaliczna, 12 butelek w szkle", 1))
        assert count == 1
        assert bd == {"szkło": 1}

    def test_szklo_2_zgrzewki_one_2pak(self):
        count, bd = self._run(("HUMIO - woda alkaliczna, 12 butelek w szkle", 2))
        assert count == 1
        assert bd == {"szkło-2pak": 1}

    def test_szklo_3_zgrzewki_2pak_plus_1pak(self):
        count, bd = self._run(("HUMIO - woda alkaliczna, 12 butelek w szkle", 3))
        assert count == 2
        assert bd == {"szkło-2pak": 1, "szkło": 1}

    def test_szklo_4_zgrzewki_two_2pak(self):
        count, bd = self._run(("HUMIO - woda alkaliczna, 12 butelek w szkle", 4))
        assert count == 2
        assert bd == {"szkło-2pak": 2}

    # ── Mieszane ─────────────────────────────────────────────────────────────

    def test_mixed_plastik_and_szklo(self):
        count, bd = self._run(
            ("HUMIO - woda alkaliczna, 12 butelek", 3),
            ("HUMIO - woda alkaliczna, 12 butelek w szkle", 1),
        )
        assert count == 2
        assert bd == {"3-pak": 1, "szkło": 1}

    def test_mixed_multiple_plastik_lines(self):
        # 2 linie plastiku: 2 + 1 = 3 zgrzewki → 1×3-pak
        count, bd = self._run(
            ("HUMIO - woda alkaliczna, 12 butelek", 2),
            ("HUMIO - woda alkaliczna, 12 butelek", 1),
        )
        assert count == 1
        assert bd == {"3-pak": 1}

    # ── Draft application integration ─────────────────────────────────────────

    def test_packages_breakdown_stored_in_draft(self, store):

        order = {
            "id": "700",
            "order_number": 8001,
            "shipping_lines": [{"title": "Apaczka"}],
            "line_items": [
                {"name": "HUMIO - woda alkaliczna, 12 butelek", "quantity": 5},
            ],
            "shipping_address": {
                "first_name": "X",
                "last_name": "Y",
                "address1": "ul. A 1",
                "address2": "",
                "city": "W",
                "zip": "00-001",
                "phone": "",
            },
            "customer": {},
            "email": "x@y.pl",
            "note_attributes": [],
        }
        _create_draft_for_test(order, store, object())
        d = store.list_drafts()[0]
        assert d["packages_count"] == 2
        bd = {b["type"]: b["qty"] for b in d["packages_breakdown"]}
        assert bd == {"3-pak": 1, "2-pak": 1}

    def test_six_bottle_order_1648_stored_as_half_pack(self, store):

        order = {
            "id": "1648",
            "order_number": 1648,
            "shipping_lines": [{"title": "InPost Kurier"}],
            "line_items": [
                {"name": "HUMIO - woda alkaliczna, 6 butelek", "quantity": 1},
            ],
            "shipping_address": {
                "first_name": "X",
                "last_name": "Y",
                "address1": "ul. A 1",
                "address2": "",
                "city": "W",
                "zip": "00-001",
                "phone": "500600700",
            },
            "customer": {},
            "email": "x@y.pl",
            "note_attributes": [],
        }
        _create_draft_for_test(order, store, object())
        d = store.list_drafts()[0]
        assert d["shopify_order_number"] == "1648"
        assert d["packages_count"] == 1
        assert d["packages_breakdown"] == [{"type": "pół-pak", "qty": 1}]


# ── Cancel raw courier id (InPost / Apaczka) ──────────────────────────────────


class TestCancelInpostShipmentEndpoint:
    def test_successful_cancel_returns_204(self, client):
        with patch("zdrovena.api.routers.webhooks.get_secret", return_value="tok"):
            with patch(
                "zdrovena.common.inpost.InPostClient.cancel_shipment", return_value=None
            ) as mock_cancel:
                resp = client.delete("/api/inpost/shipments/ship-123")
        assert resp.status_code == 204
        assert resp.content == b""
        mock_cancel.assert_called_once_with("ship-123")

    def test_409_on_business_error(self, client):
        from zdrovena.common.shipping_exceptions import InPostBusinessError

        with patch("zdrovena.api.routers.webhooks.get_secret", return_value="tok"):
            with patch(
                "zdrovena.common.inpost.InPostClient.cancel_shipment",
                side_effect=InPostBusinessError(
                    "shipment already dispatched", courier="inpost", action="cancel_shipment"
                ),
            ):
                resp = client.delete("/api/inpost/shipments/ship-404")
        assert resp.status_code == 409


class TestCancelInpostDispatchEndpoint:
    def test_successful_cancel_returns_204(self, client):
        with patch("zdrovena.api.routers.webhooks.get_secret", return_value="tok"):
            with patch(
                "zdrovena.common.inpost.InPostClient.cancel_dispatch_order", return_value=None
            ) as mock_cancel:
                resp = client.delete("/api/inpost/dispatch_orders/disp-77")
        assert resp.status_code == 204
        mock_cancel.assert_called_once_with("disp-77")

    def test_503_on_transient_error(self, client):
        from zdrovena.common.shipping_exceptions import CourierTimeoutError

        with patch("zdrovena.api.routers.webhooks.get_secret", return_value="tok"):
            with patch(
                "zdrovena.common.inpost.InPostClient.cancel_dispatch_order",
                side_effect=CourierTimeoutError(courier="inpost", action="cancel_dispatch_order"),
            ):
                resp = client.delete("/api/inpost/dispatch_orders/disp-timeout")
        assert resp.status_code == 503


class TestCancelApaczkaOrderEndpoint:
    def test_successful_cancel_returns_204(self, client):
        with patch("zdrovena.api.routers.webhooks.get_secret") as mock_get_secret:
            mock_get_secret.return_value = "tok"
            with patch(
                "zdrovena.common.apaczka.ApaczkaClient.cancel_shipment", return_value={}
            ) as mock_cancel:
                resp = client.delete("/api/apaczka/orders/ord-55")
        assert resp.status_code == 204
        mock_cancel.assert_called_once_with("ord-55")
        requested_secrets = [c.args[0] for c in mock_get_secret.call_args_list]
        assert "apaczka_service_id" not in requested_secrets

    def test_409_on_business_error(self, client):
        from zdrovena.common.shipping_exceptions import ApaczkaBusinessError

        with patch("zdrovena.api.routers.webhooks.get_secret") as mock_get_secret:
            mock_get_secret.return_value = "tok"
            with patch(
                "zdrovena.common.apaczka.ApaczkaClient.cancel_shipment",
                side_effect=ApaczkaBusinessError(
                    "already sent", courier="apaczka", action="order_cancel"
                ),
            ):
                resp = client.delete("/api/apaczka/orders/ord-gone")
        assert resp.status_code == 409
        requested_secrets = [c.args[0] for c in mock_get_secret.call_args_list]
        assert "apaczka_service_id" not in requested_secrets


# ── POST /api/shipping/sync ───────────────────────────────────────────────────


class TestSyncOrdersEndpoint:
    def test_returns_200_with_both_sources(self, client):
        allegro_stats = {"fetched": 2, "created": 1, "skipped_duplicate": 1, "errors": 0}
        shopify_stats = {
            "fetched": 3,
            "created": 2,
            "updated": 0,
            "unchanged": 1,
            "errors": 0,
        }

        mock_allegro_client = MagicMock()

        def fake_get_secret(name, required=True):
            if name == "shopify_admin_token":
                return "shpat_test"
            return "some-value"

        with patch(
            "zdrovena.api.routers.webhooks._get_allegro_client", return_value=mock_allegro_client
        ):
            with patch(
                "zdrovena.api.routers.webhooks._get_fakturownia_invoice_client"
            ) as mock_build_fakturownia:
                with patch(
                    "zdrovena.api.routers.allegro_poller.poll_orders_once",
                    return_value=allegro_stats,
                ) as mock_poll:
                    with patch(
                        "zdrovena.api.routers.webhooks._sync_shopify_orders_from_api",
                        return_value=shopify_stats,
                    ):
                        with patch(
                            "zdrovena.api.routers.webhooks.get_secret", side_effect=fake_get_secret
                        ):
                            with patch(
                                "zdrovena.api.routers.webhooks._allowed_shopify_domains",
                                return_value={"shop.myshopify.com"},
                            ):
                                resp = client.post("/api/shipping/sync")

        assert resp.status_code == 200
        body = resp.json()
        assert body["allegro"] == allegro_stats
        assert body["shopify"] == shopify_stats
        mock_build_fakturownia.assert_not_called()
        assert mock_poll.call_args.kwargs["fakturownia_client"] is None
        assert mock_poll.call_args.kwargs["retry_existing_invoices"] is False

    def test_allegro_credentials_missing_returns_error_key(self, client):
        def fake_get_secret(name, required=True):
            if name == "shopify_admin_token":
                return None
            return None

        with patch("zdrovena.api.routers.webhooks._get_allegro_client", return_value=None):
            with patch("zdrovena.api.routers.webhooks.get_secret", side_effect=fake_get_secret):
                with patch(
                    "zdrovena.api.routers.webhooks._allowed_shopify_domains",
                    return_value=set(),
                ):
                    resp = client.post("/api/shipping/sync")

        assert resp.status_code == 200
        body = resp.json()
        assert body["allegro"] == {"error": "credentials_not_configured"}
        assert body["shopify"] == {"skipped": "not_configured"}

    def test_allegro_poll_raises_exception_returns_error(self, client):
        mock_allegro_client = MagicMock()

        def fake_get_secret(name, required=True):
            return None

        with patch(
            "zdrovena.api.routers.webhooks._get_allegro_client", return_value=mock_allegro_client
        ):
            with patch(
                "zdrovena.api.routers.webhooks._get_fakturownia_invoice_client",
                return_value=None,
            ):
                with patch(
                    "zdrovena.api.routers.allegro_poller.poll_orders_once",
                    side_effect=RuntimeError("allegro API down"),
                ):
                    with patch(
                        "zdrovena.api.routers.webhooks.get_secret", side_effect=fake_get_secret
                    ):
                        with patch(
                            "zdrovena.api.routers.webhooks._allowed_shopify_domains",
                            return_value=set(),
                        ):
                            resp = client.post("/api/shipping/sync")

        assert resp.status_code == 200
        body = resp.json()
        assert "error" in body["allegro"]
        assert "allegro API down" in body["allegro"]["error"]
        assert body["shopify"] == {"skipped": "not_configured"}

    def test_shopify_sync_raises_exception_returns_error(self, client):
        allegro_stats = {"fetched": 0, "created": 0, "skipped_duplicate": 0, "errors": 0}
        mock_allegro_client = MagicMock()

        def fake_get_secret(name, required=True):
            if name == "shopify_admin_token":
                return "shpat_test"
            return "some-value"

        with patch(
            "zdrovena.api.routers.webhooks._get_allegro_client", return_value=mock_allegro_client
        ):
            with patch(
                "zdrovena.api.routers.webhooks._get_fakturownia_invoice_client",
                return_value=None,
            ):
                with patch(
                    "zdrovena.api.routers.allegro_poller.poll_orders_once",
                    return_value=allegro_stats,
                ):
                    with patch(
                        "zdrovena.api.routers.webhooks._sync_shopify_orders_from_api",
                        side_effect=ConnectionError("shopify unreachable"),
                    ):
                        with patch(
                            "zdrovena.api.routers.webhooks.get_secret", side_effect=fake_get_secret
                        ):
                            with patch(
                                "zdrovena.api.routers.webhooks._allowed_shopify_domains",
                                return_value={"shop.myshopify.com"},
                            ):
                                resp = client.post("/api/shipping/sync")

        assert resp.status_code == 200
        body = resp.json()
        assert body["allegro"] == allegro_stats
        assert "error" in body["shopify"]
        assert "shopify unreachable" in body["shopify"]["error"]

    def test_shopify_not_configured_when_no_token(self, client):
        allegro_stats = {"fetched": 0, "created": 0, "skipped_duplicate": 0, "errors": 0}
        mock_allegro_client = MagicMock()

        def fake_get_secret(name, required=True):
            if name == "shopify_admin_token":
                return None
            return "some-value"

        with patch(
            "zdrovena.api.routers.webhooks._get_allegro_client", return_value=mock_allegro_client
        ):
            with patch(
                "zdrovena.api.routers.webhooks._get_fakturownia_invoice_client",
                return_value=None,
            ):
                with patch(
                    "zdrovena.api.routers.allegro_poller.poll_orders_once",
                    return_value=allegro_stats,
                ):
                    with patch(
                        "zdrovena.api.routers.webhooks.get_secret", side_effect=fake_get_secret
                    ):
                        with patch(
                            "zdrovena.api.routers.webhooks._allowed_shopify_domains",
                            return_value={"shop.myshopify.com"},
                        ):
                            resp = client.post("/api/shipping/sync")

        assert resp.status_code == 200
        body = resp.json()
        assert body["allegro"] == allegro_stats
        assert body["shopify"] == {"skipped": "not_configured"}


# ── _sync_shopify_orders_from_api unit tests ─────────────────────────────────


class TestSyncShopifyOrdersFromApi:
    """Unit tests for _sync_shopify_orders_from_api helper."""

    def _make_order(self, order_id: int = 1001, shipping_lines=None) -> dict:
        return {
            "id": order_id,
            "order_number": order_id,
            "shipping_lines": shipping_lines or [{"title": "DPD Kurier"}],
            "shipping_address": {
                "first_name": "Jan",
                "last_name": "Kowalski",
                "address1": "Kwiatowa 1",
                "city": "Warszawa",
                "zip": "00-001",
            },
            "customer": {"email": "jan@example.com", "phone": "500000000"},
        }

    def test_empty_orders_returns_zero_stats(self, tmp_path):
        from responses import RequestsMock

        from zdrovena.api.routers.webhooks import _sync_shopify_orders_from_api
        from zdrovena.common.storage import LocalStorageService

        store = ShippingStore(local_root=tmp_path / "store")
        storage = LocalStorageService(root=tmp_path / "storage")

        with RequestsMock() as rsps:
            rsps.add(
                rsps.GET,
                "https://shop.myshopify.com/admin/api/2024-01/orders.json",
                json={"orders": []},
                status=200,
            )
            stats = _sync_shopify_orders_from_api(
                shop_domain="shop.myshopify.com",
                api_token="tok",
                shipping_store=store,
                storage=storage,
            )

        assert stats == {
            "fetched": 0,
            "created": 0,
            "updated": 0,
            "unchanged": 0,
            "errors": 0,
        }

    def test_existing_order_is_unchanged_when_payload_matches(self, tmp_path):
        from responses import RequestsMock

        from zdrovena.api.routers.webhooks import _sync_shopify_orders_from_api
        from zdrovena.common.storage import LocalStorageService

        store = ShippingStore(local_root=tmp_path / "store")
        storage = LocalStorageService(root=tmp_path / "storage")
        order = self._make_order(order_id=9999)

        with RequestsMock() as rsps:
            rsps.add(
                rsps.GET,
                "https://shop.myshopify.com/admin/api/2024-01/orders.json",
                json={"orders": [order]},
                status=200,
            )
            _create_draft_for_test(order, store, storage, source="shopify")
            stats = _sync_shopify_orders_from_api(
                shop_domain="shop.myshopify.com",
                api_token="tok",
                shipping_store=store,
                storage=storage,
            )

        assert stats["fetched"] == 1
        assert stats["unchanged"] == 1
        assert stats["created"] == 0

    def test_existing_order_status_updates_from_pending_to_fulfilled(self, tmp_path):
        from responses import RequestsMock

        from zdrovena.api.routers.webhooks import _sync_shopify_orders_from_api
        from zdrovena.common.storage import LocalStorageService

        store = ShippingStore(local_root=tmp_path / "store")
        storage = LocalStorageService(root=tmp_path / "storage")
        order = self._make_order(order_id=7777)
        _create_draft_for_test(order, store, storage, source="shopify")

        fulfilled = {
            **order,
            "fulfillment_status": "fulfilled",
            "updated_at": "2026-07-15T10:00:00Z",
            "fulfillments": [
                {
                    "id": 123,
                    "tracking_number": "TRK-777",
                    "tracking_company": "InPost",
                    "updated_at": "2026-07-15T09:59:00Z",
                }
            ],
        }
        with RequestsMock() as rsps:
            rsps.add(
                rsps.GET,
                "https://shop.myshopify.com/admin/api/2024-01/orders.json",
                json={"orders": [fulfilled]},
                status=200,
            )
            stats = _sync_shopify_orders_from_api(
                shop_domain="shop.myshopify.com",
                api_token="tok",
                shipping_store=store,
                storage=storage,
            )

        drafts = store.list_drafts()
        assert stats["updated"] == 1
        assert len(drafts) == 1
        assert drafts[0]["status"] == "created"
        assert drafts[0]["fulfillment_status"] == "fulfilled"
        assert drafts[0]["fulfilled_at"] == "2026-07-15T09:59:00Z"
        assert drafts[0]["tracking_number"] == "TRK-777"
        assert drafts[0]["tracking_company"] == "InPost"

    def test_sync_updates_original_instead_of_replacement_draft(self, tmp_path):
        from responses import RequestsMock

        from zdrovena.api.routers.webhooks import _sync_shopify_orders_from_api
        from zdrovena.common.storage import LocalStorageService

        store = ShippingStore(local_root=tmp_path / "store")
        storage = LocalStorageService(root=tmp_path / "storage")
        order = self._make_order(order_id=4242)
        original = _create_draft_for_test(order, store, storage, source="shopify")
        replacement = {
            **original,
            "id": "replacement-4242",
            "created_at": "2026-07-16T10:00:00Z",
            "shopify_order_id": None,
            "status": "needs_review",
            "is_replacement": True,
            "tracking_number": None,
        }
        store.upsert_draft(replacement)
        fulfilled = {
            **order,
            "fulfillment_status": "fulfilled",
            "updated_at": "2026-07-17T10:00:00Z",
            "fulfillments": [
                {
                    "id": 4243,
                    "tracking_number": "TRK-ORIGINAL",
                    "tracking_company": "InPost",
                }
            ],
        }

        with RequestsMock() as rsps:
            rsps.add(
                rsps.GET,
                "https://shop.myshopify.com/admin/api/2024-01/orders.json",
                json={"orders": [fulfilled]},
                status=200,
            )
            stats = _sync_shopify_orders_from_api(
                shop_domain="shop.myshopify.com",
                api_token="tok",
                shipping_store=store,
                storage=storage,
            )

        assert stats["updated"] == 1
        assert store.get_draft(original["id"])["tracking_number"] == "TRK-ORIGINAL"
        assert store.get_draft("replacement-4242")["tracking_number"] is None

    def test_sync_queries_any_status_ordered_by_recent_updates(self, tmp_path):
        import json
        from urllib.parse import parse_qs, urlparse

        from responses import RequestsMock

        from zdrovena.api.routers.webhooks import _sync_shopify_orders_from_api
        from zdrovena.common.storage import LocalStorageService

        store = ShippingStore(local_root=tmp_path / "store")
        storage = LocalStorageService(root=tmp_path / "storage")
        request_urls: list[str] = []

        def capture_request(request):
            request_urls.append(request.url)
            return 200, {}, json.dumps({"orders": []})

        with RequestsMock() as rsps:
            rsps.add_callback(
                rsps.GET,
                "https://shop.myshopify.com/admin/api/2024-01/orders.json",
                callback=capture_request,
            )
            _sync_shopify_orders_from_api(
                shop_domain="shop.myshopify.com",
                api_token="tok",
                shipping_store=store,
                storage=storage,
            )

        query = parse_qs(urlparse(request_urls[0]).query)
        assert query["status"] == ["any"]
        assert query["fulfillment_status"] == ["any"]
        assert query["order"] == ["updated_at desc"]

    def test_sync_does_not_regress_created_draft_to_pending(self, tmp_path):
        from responses import RequestsMock

        from zdrovena.api.routers.webhooks import _sync_shopify_orders_from_api
        from zdrovena.common.storage import LocalStorageService

        store = ShippingStore(local_root=tmp_path / "store")
        storage = LocalStorageService(root=tmp_path / "storage")
        order = self._make_order(order_id=8888)
        store.upsert_draft(
            {
                "id": "existing-created",
                "created_at": "2026-07-01T00:00:00+00:00",
                "source": "shopify",
                "external_order_id": "8888",
                "shopify_order_id": "8888",
                "shopify_order_number": "8888",
                "customer_name": "Jan Kowalski",
                "courier": "inpost",
                "service": "inpost_courier_standard",
                "status": "created",
                "tracking_number": "TRK-OLD",
                "courier_draft_id": "SHIP-OLD",
                "pickup_ordered": False,
            }
        )

        with RequestsMock() as rsps:
            rsps.add(
                rsps.GET,
                "https://shop.myshopify.com/admin/api/2024-01/orders.json",
                json={"orders": [order]},
                status=200,
            )
            stats = _sync_shopify_orders_from_api(
                shop_domain="shop.myshopify.com",
                api_token="tok",
                shipping_store=store,
                storage=storage,
            )

        draft = store.get_draft("existing-created")
        assert stats["updated"] == 1
        assert draft["status"] == "created"
        assert draft["tracking_number"] == "TRK-OLD"
        assert draft["courier_draft_id"] == "SHIP-OLD"

    def test_http_error_propagates(self, tmp_path):
        import requests
        from responses import RequestsMock

        from zdrovena.api.routers.webhooks import _sync_shopify_orders_from_api
        from zdrovena.common.storage import LocalStorageService

        store = ShippingStore(local_root=tmp_path / "store")
        storage = LocalStorageService(root=tmp_path / "storage")

        with RequestsMock() as rsps:
            rsps.add(
                rsps.GET,
                "https://shop.myshopify.com/admin/api/2024-01/orders.json",
                json={"errors": "Unauthorized"},
                status=401,
            )
            with pytest.raises(requests.HTTPError):
                _sync_shopify_orders_from_api(
                    shop_domain="shop.myshopify.com",
                    api_token="bad-token",
                    shipping_store=store,
                    storage=storage,
                )


class TestBatchLabels:
    """R5-B: POST /shipping/labels/batch — merge selected labels into one PDF,
    with deterministic errors for missing / not-ready / oversized batches."""

    @staticmethod
    def _valid_pdf() -> bytes:
        from pypdf import PdfWriter

        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        buf = io.BytesIO()
        writer.write(buf)
        return buf.getvalue()

    def _seed(self, store, draft_id, courier="inpost", courier_draft_id="cd-1"):
        store.upsert_draft(
            {
                "id": draft_id,
                "source": "shopify",
                "status": "created",
                "courier": courier,
                "courier_draft_id": courier_draft_id,
                "shopify_order_number": draft_id,
            }
        )

    def test_400_on_empty(self, client):
        resp = client.post("/api/shipping/labels/batch", json={"draft_ids": []})
        assert resp.status_code == 400

    def test_404_on_missing_draft(self, client, store):
        self._seed(store, "b-1")
        resp = client.post("/api/shipping/labels/batch", json={"draft_ids": ["b-1", "ghost"]})
        assert resp.status_code == 404
        assert "ghost" in resp.json()["detail"]

    def test_merges_labels_into_single_pdf(self, client, store):
        self._seed(store, "b-1", courier_draft_id="cd-1")
        self._seed(store, "b-2", courier_draft_id="cd-2")
        pdf = self._valid_pdf()
        with patch("zdrovena.api.routers.webhooks.get_secret", return_value="tok"):
            with patch("zdrovena.common.inpost.InPostClient.get_label", return_value=pdf):
                resp = client.post("/api/shipping/labels/batch", json={"draft_ids": ["b-1", "b-2"]})
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.content[:4] == b"%PDF"

    def test_409_when_label_not_ready(self, client, store):
        from zdrovena.common.shipping_exceptions import InPostBusinessError

        self._seed(store, "b-1", courier_draft_id="cd-1")
        with patch("zdrovena.api.routers.webhooks.get_secret", return_value="tok"):
            with patch(
                "zdrovena.common.inpost.InPostClient.get_label",
                side_effect=InPostBusinessError("shipment not confirmed", courier="inpost"),
            ):
                resp = client.post("/api/shipping/labels/batch", json={"draft_ids": ["b-1"]})
        assert resp.status_code == 409
        assert "b-1" in resp.json()["detail"]

    def test_409_when_draft_has_no_label_id(self, client, store):
        self._seed(store, "b-1", courier_draft_id=None)
        resp = client.post("/api/shipping/labels/batch", json={"draft_ids": ["b-1"]})
        assert resp.status_code == 409

    def test_400_when_over_limit(self, client, store):
        ids = [f"x-{i}" for i in range(101)]
        resp = client.post("/api/shipping/labels/batch", json={"draft_ids": ids})
        assert resp.status_code == 400


class TestSingleLabelNotReady:
    def test_inpost_business_error_maps_to_409(self, client, store):
        from zdrovena.common.shipping_exceptions import InPostBusinessError

        store.upsert_draft(
            {
                "id": "lnr-1",
                "source": "shopify",
                "status": "created",
                "courier": "inpost",
                "courier_draft_id": "cd-1",
                "shopify_order_number": "lnr-1",
            }
        )
        with patch("zdrovena.api.routers.webhooks.get_secret", return_value="tok"):
            with patch(
                "zdrovena.common.inpost.InPostClient.get_label",
                side_effect=InPostBusinessError("not confirmed yet", courier="inpost"),
            ):
                resp = client.get("/api/shipping/drafts/lnr-1/label?courier=inpost")
        # R5-B: pre-confirmation → 409 LABEL_NOT_READY, not a generic 502.
        assert resp.status_code == 409
        assert resp.json()["error_code"] == "LabelNotReadyError"


class TestPickupAddressSecrets:
    """`pickup_phone` was never provisioned in the prod Key Vault, so every
    dispatch-order execute raised MissingSecretError and surfaced as a 502.
    A missing pickup phone must degrade to the sender phone, not hard-fail."""

    _PRESENT: ClassVar[dict[str, str]] = {
        "pickup_name": "Zdrovena Magazyn",
        "pickup_street": "Testowa",
        "pickup_building_number": "1",
        "pickup_city": "Warszawa",
        "pickup_post_code": "00-001",
        "pickup_email": "magazyn@zdrovena.pl",
        "sender_phone": "500000000",
    }

    def _fake_get_secret(self, missing):
        from zdrovena.common.exceptions import MissingSecretError

        def _inner(service, required=True):
            if service in missing:
                if required:
                    raise MissingSecretError(service, "humio")
                return None
            return self._PRESENT.get(service, "x")

        return _inner

    def test_missing_pickup_phone_falls_back_to_sender_phone(self):
        from zdrovena.api.routers import webhooks as wh

        with patch.object(wh, "get_secret", self._fake_get_secret({"pickup_phone"})):
            addr = wh._get_pickup_address()
        assert addr["phone"] == "500000000"

    def test_present_pickup_phone_wins_over_sender_phone(self):
        from zdrovena.api.routers import webhooks as wh

        present = dict(self._PRESENT, pickup_phone="600111222")

        def _get(service, required=True):
            return present.get(service, "x")

        with patch.object(wh, "get_secret", _get):
            addr = wh._get_pickup_address()
        assert addr["phone"] == "600111222"

    def test_other_pickup_fields_still_required(self):
        """Only the phone degrades; a missing street is still a hard error."""
        from zdrovena.api.routers import webhooks as wh
        from zdrovena.common.exceptions import MissingSecretError

        with patch.object(wh, "get_secret", self._fake_get_secret({"pickup_street"})):
            with pytest.raises(MissingSecretError):
                wh._get_pickup_address()


def _seed_origin_draft(store):
    """An executable draft, mirroring TestExecuteDraft._seed_error_draft."""
    draft = {
        "id": "draft-origin-1",
        "created_at": "2026-05-20T10:00:00+00:00",
        "source": "shopify",
        "shopify_order_id": "77",
        "shopify_order_number": "1177",
        "customer_name": "Test User",
        "courier": "inpost",
        "service": "inpost_courier_standard",
        "tracking_number": None,
        "courier_draft_id": None,
        "status": "error",
        "packages_count": 1,
        "pickup_ordered": False,
        "receiver": {
            "first_name": "Test",
            "last_name": "User",
            "email": "t@t.com",
            "phone": "500000000",
            "locker_id": "WAW01A",
        },
        "shipping_address": {"street": "Kwiatowa 1", "city": "Warszawa", "post_code": "00-001"},
        "parcel": {"template": "small", "weight_kg": None},
        "error": "no credentials",
    }
    store.upsert_draft(draft)
    return draft


class TestPendingResolvedByTheShop:
    """The shop is the source of truth for shipments today: the operator creates
    them in Shopify or Apaczka and the portal follows. A draft waiting on an
    InPost confirmation must yield to a tracking number arriving from there."""

    def test_tracking_from_the_shop_clears_pending_confirmation(self):

        existing = {"id": "d1", "status": "pending_confirmation", "tracking_number": ""}
        incoming = {"id": "d1", "status": "created", "tracking_number": "TRK-MANUAL"}
        merged = _merge_synced_draft_for_test(existing, incoming)
        assert merged["status"] == "created"
        assert merged["tracking_number"] == "TRK-MANUAL"

    def test_pending_survives_a_sync_that_brings_no_tracking(self):
        """Without a number there is no proof the parcel exists, so the draft
        keeps waiting rather than claiming to be sent."""

        existing = {"id": "d2", "status": "pending_confirmation", "tracking_number": ""}
        incoming = {"id": "d2", "status": "created", "tracking_number": ""}
        merged = _merge_synced_draft_for_test(existing, incoming)
        assert merged["status"] == "pending_confirmation"

    def test_executing_is_never_released_by_a_sync(self):
        """executing is a concurrency claim, not a wait — a shop tracking number
        must not unlock a draft another request is mid-way through."""

        existing = {"id": "d3", "status": "executing", "tracking_number": ""}
        incoming = {"id": "d3", "status": "created", "tracking_number": "TRK-MANUAL"}
        merged = _merge_synced_draft_for_test(existing, incoming)
        assert merged["status"] == "executing"


class TestShipmentOrigin:
    """126 drafts carry tracking numbers this system never created, because the
    operator dispatches through carrier portals and the sync writes the number
    back. Status 'created' therefore says nothing about who shipped it."""

    def test_sync_marks_tracking_we_did_not_create_as_external(self):

        existing = {"id": "d1", "tracking_number": None, "courier_draft_id": None}
        incoming = {"id": "d1", "tracking_number": "TRK-MANUAL", "status": "created"}
        merged = _merge_synced_draft_for_test(existing, incoming)
        assert merged["shipment_origin"] == "external"

    def test_sync_marks_our_own_shipment_as_system(self):

        existing = {"id": "d2", "tracking_number": "TRK1", "courier_draft_id": "ship-1"}
        incoming = {"id": "d2", "tracking_number": "TRK1", "status": "created"}
        merged = _merge_synced_draft_for_test(existing, incoming)
        assert merged["shipment_origin"] == "system"

    def test_sync_does_not_invent_an_origin_without_tracking(self):

        existing = {"id": "d3", "tracking_number": None, "courier_draft_id": None}
        incoming = {"id": "d3", "tracking_number": None, "status": "pending"}
        merged = _merge_synced_draft_for_test(existing, incoming)
        assert merged.get("shipment_origin") is None

    def test_sync_never_downgrades_a_recorded_origin(self):

        existing = {
            "id": "d4",
            "tracking_number": "TRK1",
            "courier_draft_id": None,
            "shipment_origin": "system",
        }
        incoming = {"id": "d4", "tracking_number": "TRK1", "status": "created"}
        merged = _merge_synced_draft_for_test(existing, incoming)
        assert merged["shipment_origin"] == "system"

    def test_execute_records_system_origin(self, client, store):
        draft = _seed_origin_draft(store)
        with patch(
            "zdrovena.api.routers.webhooks._run_inpost",
            return_value={
                "courier_draft_id": "ship-1",
                "tracking_number": "TRK1",
                "status": "created",
                "error": None,
            },
        ):
            resp = client.post(f"/api/shipping/drafts/{draft['id']}/execute")
        assert resp.status_code == 200, resp.text
        assert store.get_draft(draft["id"])["shipment_origin"] == "system"

    def test_execute_emits_tracking_assigned_event(self, client, store, caplog):
        draft = _seed_origin_draft(store)
        with caplog.at_level(logging.INFO, logger="zdrovena.events"):
            with patch(
                "zdrovena.api.routers.webhooks._run_inpost",
                return_value={
                    "courier_draft_id": "ship-1",
                    "tracking_number": "TRK1",
                    "status": "created",
                    "error": None,
                },
            ):
                client.post(f"/api/shipping/drafts/{draft['id']}/execute")

        events = [
            r.getMessage() for r in caplog.records if "draft.tracking_assigned" in r.getMessage()
        ]
        assert events, "the no-tracking alert depends on this event existing"
        assert "system" in events[0]


class TestTrackingAssignedCoversEveryPath:
    """Codex review, 2026-07-31: the alert joins draft.created against
    draft.tracking_assigned, so any path that assigns a tracking number without
    emitting the event makes that draft look unshipped and pages falsely.
    The manual-portal path is the common one — 126 of 197 drafts."""

    def test_sync_assigning_external_tracking_emits_the_event(self, caplog):

        existing = {"id": "d1", "tracking_number": None, "courier_draft_id": None}
        incoming = {"id": "d1", "tracking_number": "TRK-MANUAL", "status": "created"}
        with caplog.at_level(logging.INFO, logger="zdrovena.events"):
            _merge_synced_draft_for_test(existing, incoming)

        events = [
            r.getMessage() for r in caplog.records if "draft.tracking_assigned" in r.getMessage()
        ]
        assert events, "a manually shipped draft would otherwise page as untracked"
        assert "external" in events[0]

    def test_sync_does_not_re_emit_for_an_already_known_origin(self, caplog):

        existing = {
            "id": "d2",
            "tracking_number": "TRK1",
            "courier_draft_id": None,
            "shipment_origin": "external",
        }
        incoming = {"id": "d2", "tracking_number": "TRK1", "status": "created"}
        with caplog.at_level(logging.INFO, logger="zdrovena.events"):
            _merge_synced_draft_for_test(existing, incoming)

        events = [
            r.getMessage() for r in caplog.records if "draft.tracking_assigned" in r.getMessage()
        ]
        assert not events, "the event marks assignment, not every subsequent sync"

    def test_allegro_async_confirmation_is_marked_system(self, client, store, caplog):
        """Ship-with-Allegro can return pending_confirmation; the waybill then
        arrives through /confirm, which bypasses the execute path entirely."""
        draft = _seed_origin_draft(store)
        store.update_draft(
            draft["id"], {"status": "pending_confirmation", "allegro_command_id": "cmd-1"}
        )

        allegro = MagicMock()
        allegro.get_ship_with_allegro_command_status.return_value = {
            "status": "SUCCESS",
            "shipmentId": "allegro-ship-1",
        }
        allegro.get_ship_with_allegro_shipment.return_value = {"id": "allegro-ship-1"}
        allegro.extract_shipment_waybill.return_value = ("carrier-1", "AWB-1")

        with caplog.at_level(logging.INFO, logger="zdrovena.events"):
            with patch("zdrovena.api.routers.webhooks._get_allegro_client", return_value=allegro):
                with patch("zdrovena.api.routers.webhooks._maybe_push_tracking_to_allegro"):
                    resp = client.post(f"/api/shipping/drafts/{draft['id']}/confirm")

        assert resp.status_code == 200, resp.text
        assert store.get_draft(draft["id"])["shipment_origin"] == "system"
        events = [
            r.getMessage() for r in caplog.records if "draft.tracking_assigned" in r.getMessage()
        ]
        assert events, "an Allegro waybill is a tracking number like any other"

    def test_inpost_confirm_promotes_draft_once_shipx_returns_tracking(self, client, store):
        """InPost parks at pending_confirmation until ShipX has a tracking
        number; /confirm is what promotes it, without POSTing a second time."""
        draft = _seed_origin_draft(store)
        store.update_draft(
            draft["id"], {"status": "pending_confirmation", "courier_draft_id": "ship-77"}
        )

        with patch("zdrovena.api.routers.webhooks.get_secret", return_value="tok"):
            with patch(
                "zdrovena.common.inpost.InPostClient.get_shipment",
                return_value={"id": "ship-77", "tracking_number": "620DONE"},
            ):
                with patch("zdrovena.common.inpost.InPostClient.create_kurier_shipment") as create:
                    resp = client.post(f"/api/shipping/drafts/{draft['id']}/confirm")

        assert resp.status_code == 200, resp.text
        create.assert_not_called()
        updated = store.get_draft(draft["id"])
        assert updated["status"] == "created"
        assert updated["tracking_number"] == "620DONE"
        assert updated["shipment_origin"] == "system"

    def test_inpost_confirm_refreshes_all_parcels_and_preserves_pickup(self, client, store):
        draft = _seed_origin_draft(store)
        store.update_draft(
            draft["id"],
            {
                "status": "pending_confirmation",
                "courier_draft_id": "ship-1",
                "dispatch_order_id": "dispatch-123",
                "pickup_ordered": True,
                "courier_shipments": [
                    {
                        "id": "ship-1",
                        "tracking_number": "TRACK-1",
                        "package_type": "1-pak",
                        "package_number": "1",
                    },
                    {
                        "id": "ship-2",
                        "tracking_number": "",
                        "package_type": "1-pak",
                        "package_number": "2",
                    },
                ],
            },
        )

        def get_shipment(shipment_id: str) -> dict[str, str]:
            assert shipment_id == "ship-2"
            return {"id": "ship-2", "tracking_number": "TRACK-2"}

        with patch("zdrovena.api.routers.webhooks.get_secret", return_value="tok"):
            with patch(
                "zdrovena.common.inpost.InPostClient.get_shipment",
                side_effect=get_shipment,
            ):
                with patch("zdrovena.common.inpost.InPostClient.create_kurier_shipment") as create:
                    resp = client.post(f"/api/shipping/drafts/{draft['id']}/confirm")

        assert resp.status_code == 200, resp.text
        create.assert_not_called()
        updated = store.get_draft(draft["id"])
        assert [item["tracking_number"] for item in updated["courier_shipments"]] == [
            "TRACK-1",
            "TRACK-2",
        ]
        assert updated["status"] == "created"
        assert updated["dispatch_order_id"] == "dispatch-123"
        assert updated["pickup_ordered"] is True

    def test_inpost_confirm_stays_202_while_shipx_has_no_tracking(self, client, store):
        """Still waiting is not an error and must not flip the draft to created."""
        draft = _seed_origin_draft(store)
        store.update_draft(
            draft["id"], {"status": "pending_confirmation", "courier_draft_id": "ship-78"}
        )

        with patch("zdrovena.api.routers.webhooks.get_secret", return_value="tok"):
            with patch(
                "zdrovena.common.inpost.InPostClient.get_shipment",
                return_value={"id": "ship-78", "tracking_number": None},
            ):
                with patch(
                    "zdrovena.common.inpost.InPostClient.wait_for_shipment_confirmation",
                    return_value={"id": "ship-78", "tracking_number": None},
                ):
                    resp = client.post(f"/api/shipping/drafts/{draft['id']}/confirm")

        assert resp.status_code == 202, resp.text
        assert store.get_draft(draft["id"])["status"] == "pending_confirmation"


_APACZKA_DRAFT = {
    "id": "d-apaczka",
    "shopify_order_number": "1052",
    "courier": "apaczka",
    "service": "apaczka",
    "apaczka_service_id": "42",
    "receiver": {
        "first_name": "Ewa",
        "last_name": "Zielinska",
        "email": "ewa@z.pl",
        "phone": "600300400",
        "locker_id": "",
    },
    "shipping_address": {
        "street": "Polna",
        "building_number": "7",
        "city": "Gdansk",
        "post_code": "80-001",
    },
}

_PICKUP = {
    "name": "Zdrovena",
    "firstname": "",
    "lastname": "Zdrovena",
    "email": "info@wodahumio.pl",
    "phone": "723624437",
    "street": "Naściszowa",
    "building_number": "41",
    "city": "Naściszowa",
    "post_code": "33-300",
}


class TestApaczkaPayloadPlan:
    """Apaczka is the courier that actually ships today, so its preview has to
    be as trustworthy as InPost's — same identity guarantee, same boundary."""

    def test_plan_lists_one_payload_per_parcel_and_sends_nothing(self):
        with patch("zdrovena.common.apaczka.ApaczkaClient._call") as mock_call:
            plan = _provider_apaczka_payload_plan(_APACZKA_DRAFT, _PICKUP)

        assert len(plan) >= 1
        assert "payload" in plan[0]
        mock_call.assert_not_called()

    def test_fixed_payload_plan_matches_golden(self):
        draft = {
            **_APACZKA_DRAFT,
            "packages_breakdown": [{"type": "pół-pak", "qty": 1}],
            "order_items": [{"name": "HUMIO 500 ml", "quantity": 2}],
        }

        assert _provider_apaczka_payload_plan(draft, _PICKUP) == [
            {
                "service": "apaczka",
                "package_type": "pół-pak",
                "package_number": 1,
                "reference": "1052 | plastik | pół-pak",
                "payload": {
                    "service_id": "42",
                    "externalId": "1052 | plastik | pół-pak",
                    "address": {
                        "sender": {
                            "name": "Zdrovena",
                            "contact_person": "Zdrovena",
                            "email": "info@wodahumio.pl",
                            "phone": "723624437",
                            "line1": "Naściszowa 41",
                            "line2": "",
                            "city": "Naściszowa",
                            "postal_code": "33-300",
                            "country_code": "PL",
                        },
                        "receiver": {
                            "name": "Ewa Zielinska",
                            "contact_person": "Ewa Zielinska",
                            "email": "ewa@z.pl",
                            "phone": "600300400",
                            "line1": "Polna 7",
                            "line2": "",
                            "city": "Gdansk",
                            "postal_code": "80-001",
                            "country_code": "PL",
                        },
                    },
                    "shipment": [
                        {
                            "weight": 3.0,
                            "dimension1": 20,
                            "dimension2": 15,
                            "dimension3": 20,
                            "is_nstd": 0,
                            "shipment_type_code": "PACZKA",
                        }
                    ],
                    "pickup": {"type": "COURIER"},
                    "content": "2 x HUMIO 500 ml",
                },
            }
        ]

    def test_preview_payload_is_what_the_execution_path_sends(self):
        """The preview is worthless if it can differ from the real request."""
        from zdrovena.api.routers import webhooks as wh

        with patch("zdrovena.api.routers.webhooks.get_secret", return_value="x"):
            plan = _provider_apaczka_payload_plan(_APACZKA_DRAFT, _PICKUP)

            with patch(
                "zdrovena.common.apaczka.ApaczkaClient._call",
                return_value={"response": {"order": {"id": "ap-1", "waybill_number": "W1"}}},
            ) as mock_call:
                wh._run_apaczka(_APACZKA_DRAFT, _PICKUP, MagicMock())

        sent = [c.args[1]["order"] for c in mock_call.call_args_list]
        assert sent == [entry["payload"] for entry in plan]

    def test_sender_on_the_payload_is_the_pickup_address(self):
        """Deliberate asymmetry with InPost — Naściszowa, not Kraków."""
        plan = _provider_apaczka_payload_plan(_APACZKA_DRAFT, _PICKUP)

        sender = plan[0]["payload"]["address"]["sender"]
        assert sender["city"] == "Naściszowa"
        assert sender["postal_code"] == "33-300"


class TestApaczkaPreviewEndpoint:
    def _seed(self, store):
        draft = dict(_APACZKA_DRAFT, id="draft-apz-preview", status="error")
        store.upsert_draft(draft)
        return draft

    def test_apaczka_preview_returns_real_payloads(self, client, store):
        draft = self._seed(store)
        with patch("zdrovena.api.routers.webhooks._get_pickup_address", return_value=_PICKUP):
            resp = client.get(f"/api/shipping/drafts/{draft['id']}/execute/preview")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["courier"] == "apaczka"
        assert body["parcels"], "Apaczka is the courier that actually ships — it needs a preview"
        assert "payload" in body["parcels"][0]
        assert "note" not in body

    def test_apaczka_preview_sender_is_the_pickup_address(self, client, store):
        draft = self._seed(store)
        with patch("zdrovena.api.routers.webhooks._get_pickup_address", return_value=_PICKUP):
            body = client.get(f"/api/shipping/drafts/{draft['id']}/execute/preview").json()
        assert body["sender"]["city"] == "Naściszowa"

    def test_allegro_preview_renders_the_fetched_payload(self, client, store):
        draft = dict(_ALLEGRO_DRAFT, id="draft-allegro-preview", status="error")
        store.upsert_draft(draft)
        allegro = MagicMock()
        allegro.get_delivery_proposal.return_value = _ALLEGRO_PROPOSAL

        with patch("zdrovena.api.routers.webhooks._get_allegro_client", return_value=allegro):
            body = client.get(f"/api/shipping/drafts/{draft['id']}/execute/preview").json()

        assert body["preview_available"] is True
        assert body["parcels"][0]["payload"]["receiver"]["name"] == "Ola Wisniewska"
        allegro.create_ship_with_allegro_shipment.assert_not_called()

    def test_allegro_outage_blocks_confirmation_instead_of_hiding(self, client, store):
        """The execute path calls the same endpoint, so failing closed costs
        nothing that was not already lost — and it stops the operator
        confirming a shipment that could never have been created."""
        from zdrovena.common.shipping_exceptions import CourierTransientError

        draft = dict(_ALLEGRO_DRAFT, id="draft-allegro-down", status="error")
        store.upsert_draft(draft)
        allegro = MagicMock()
        allegro.get_delivery_proposal.side_effect = CourierTransientError(
            "Allegro unavailable", courier="allegro", action="delivery_proposal"
        )

        with patch("zdrovena.api.routers.webhooks._get_allegro_client", return_value=allegro):
            resp = client.get(f"/api/shipping/drafts/{draft['id']}/execute/preview")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["preview_available"] is False
        assert body["parcels"] == []
        assert "Allegro" in body["note"]


_ALLEGRO_DRAFT = {
    "id": "d-allegro",
    "shopify_order_number": "1053",
    "external_order_id": "allegro-order-9",
    "courier": "allegro_delivery",
    "service": "allegro_delivery",
    "receiver": {"first_name": "Ola", "last_name": "Wisniewska", "locker_id": ""},
    "shipping_address": {"street": "Lipowa", "building_number": "3", "city": "Lodz"},
}

# Allegro returns the prefilled address blocks under `suggestedInput`. There is
# no senderData/receiverData in the documented response — this fixture used to
# claim there was, which is the same wrong shape that shipped empty sender
# blocks to the create-commands endpoint in production.
_ALLEGRO_PROPOSAL = {
    "suggestedInput": {
        "sender": {"name": "Maria Gryzło ZDROVENA", "street": "Cieszynska 6/12"},
        "receiver": {"name": "Ola Wisniewska", "street": "Lipowa 3", "city": "Lodz"},
    }
}


class TestAllegroPayloadPlan:
    """Allegro's payload is only knowable after asking Allegro, so the preview
    makes one read-only GET. The execute path calls the same endpoint, so
    failing closed costs nothing that was not already lost."""

    def _client(self):
        c = MagicMock()
        c.get_delivery_proposal.return_value = _ALLEGRO_PROPOSAL
        return c

    def test_plan_fetches_the_proposal_and_creates_nothing(self):
        client = self._client()
        plan = allegro_delivery_payload_plan(_ALLEGRO_DRAFT, client)

        client.get_delivery_proposal.assert_called_once_with("allegro-order-9")
        client.create_ship_with_allegro_shipment.assert_not_called()
        assert plan and "payload" in plan[0]
        assert plan[0]["payload"]["receiver"]["name"] == "Ola Wisniewska"

    def test_fixed_proposal_payload_matches_golden(self):
        draft = {
            **_ALLEGRO_DRAFT,
            "packages_breakdown": [
                {"type": "2-pak", "qty": 2},
                {"type": "pół-pak", "qty": 1},
            ],
        }
        client = self._client()
        plan = allegro_delivery_payload_plan(draft, client)

        assert plan == [
            {
                "service": "allegro_delivery",
                "package_type": "allegro",
                "package_number": 1,
                "reference": "1053 | plastik | 2-pak 1/2",
                "payload": {
                    "reference_number": "1053 | plastik | 2-pak 1/2",
                    "delivery_method_id": None,
                    "credentials_id": None,
                    "packages": [
                        {
                            "type": "PACKAGE",
                            "length": {"value": 40, "unit": "CENTIMETER"},
                            "width": {"value": 30, "unit": "CENTIMETER"},
                            "height": {"value": 20, "unit": "CENTIMETER"},
                            "weight": {"value": 27.0, "unit": "KILOGRAMS"},
                        }
                    ],
                    "sender": {
                        "name": "Maria Gryzło ZDROVENA",
                        "street": "Cieszynska 6/12",
                    },
                    "receiver": {
                        "name": "Ola Wisniewska",
                        "street": "Lipowa 3",
                        "city": "Lodz",
                    },
                    "additional_properties": None,
                },
            }
        ]
        client.get_delivery_proposal.assert_called_once_with("allegro-order-9")
        client.create_ship_with_allegro_shipment.assert_not_called()

    def test_preview_payload_is_what_the_execution_path_sends(self):
        """Identical but for command_id, which is a fresh idempotency key per
        send and therefore cannot match by design."""
        from zdrovena.api.routers import webhooks as wh

        client = self._client()
        plan = allegro_delivery_payload_plan(_ALLEGRO_DRAFT, client)
        with patch("zdrovena.api.routers.webhooks._get_allegro_client", return_value=client):
            client.wait_for_ship_with_allegro_shipment.return_value = "ship-9"
            client.get_ship_with_allegro_shipment.return_value = {}
            client.extract_shipment_waybill.return_value = ("c", "AWB-9")
            wh._run_allegro_delivery(_ALLEGRO_DRAFT, MagicMock())

        sent = dict(client.create_ship_with_allegro_shipment.call_args.kwargs)
        sent.pop("command_id")
        assert sent == plan[0]["payload"]

    def test_plan_surfaces_an_allegro_outage_instead_of_guessing(self):
        from zdrovena.common.shipping_exceptions import CourierTransientError

        client = self._client()
        client.get_delivery_proposal.side_effect = CourierTransientError(
            "Allegro down", courier="allegro", action="delivery_proposal"
        )
        with pytest.raises(CourierTransientError):
            allegro_delivery_payload_plan(_ALLEGRO_DRAFT, client)
