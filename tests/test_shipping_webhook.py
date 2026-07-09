"""Tests for zdrovena.api.routers.webhooks — HMAC validation, courier routing, and endpoints."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("AZURE_AUTH_DISABLED", "true")

from zdrovena.api.main import app
from zdrovena.api.routers.webhooks import _pick_courier, _verify_shopify_hmac
from zdrovena.common.shipping_store import ShippingStore
from zdrovena.common.shopify_dedup_store import ShopifyDedupStore

_FIXTURES = Path(__file__).parent / "fixtures"

_WEBHOOK_SECRET = "test-webhook-secret"


def _load_fixture(name: str) -> dict:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


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
            with patch("zdrovena.api.routers.webhooks._create_draft"):
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
            with patch("zdrovena.api.routers.webhooks._create_draft"):
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
            with patch("zdrovena.api.routers.webhooks._create_draft"):
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
            with patch("zdrovena.api.routers.webhooks._create_draft") as mock_create:
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
            with patch("zdrovena.api.routers.webhooks._create_draft"):
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


class TestExecuteDraft:
    def _seed_error_draft(self, store, courier="inpost", service="inpost_courier_standard"):
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


# ── Order pickup ──────────────────────────────────────────────────────────────


class TestOrderPickup:
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
        draft = self._seed_created_kurier(store)
        store.update_draft(draft["id"], {"courier": "apaczka", "service": "apaczka"})
        resp = client.post(f"/api/shipping/drafts/{draft['id']}/pickup")
        assert resp.status_code == 400

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

    def test_successful_pickup_sets_flag(self, client, store):
        draft = self._seed_created_kurier(store)
        with patch(
            "zdrovena.common.inpost.InPostClient.create_dispatch_order",
            return_value={"id": "disp-1"},
        ):
            with patch("zdrovena.api.routers.webhooks.get_secret", return_value="test-value"):
                resp = client.post(f"/api/shipping/drafts/{draft['id']}/pickup")
        assert resp.status_code == 200
        assert resp.json()["status"] == "pickup_ordered"
        updated = store.get_draft(draft["id"])
        assert updated["pickup_ordered"] is True

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


class TestRunInpost:
    def test_kurier_creates_shipment_and_dispatch(self):
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
        mock_disp.assert_called_once_with(
            "ship-1", _SENDER, pickup_date=None, pickup_from=None, pickup_to=None
        )

    def test_kurier_dispatch_failure_is_logged_not_raised(self):
        from zdrovena.api.routers.webhooks import _run_inpost

        with patch("zdrovena.api.routers.webhooks.get_secret", return_value="tok"):
            with patch("zdrovena.common.inpost.InPostClient.create_kurier_shipment") as mock_ship:
                with patch(
                    "zdrovena.common.inpost.InPostClient.create_dispatch_order",
                    side_effect=Exception("dispatch fail"),
                ):
                    mock_ship.return_value = {"id": "ship-2", "tracking_number": "TRK2"}
                    result = _run_inpost(_KURIER_DRAFT, _SENDER)
        assert result["status"] == "created"

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


class TestCreateDraft:
    def test_inpost_kurier_draft_stored_on_success(self, store, tmp_path):
        from zdrovena.api.routers.webhooks import _create_draft

        storage = object()
        order = _load_fixture("shopify_order_inpost_kurier.json")
        _create_draft(order, store, storage)
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

    def test_locker_id_from_address2_fallback(self, store):
        from zdrovena.api.routers.webhooks import _create_draft

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
        _create_draft(order, store, storage)
        drafts = store.list_drafts()
        assert len(drafts) == 1
        d = drafts[0]
        assert d["status"] == "pending"
        assert d["service"] == "inpost_locker_standard"
        assert d["receiver"]["locker_id"] == "WAW01A"


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
        from zdrovena.api.routers.webhooks import _create_draft

        storage = object()
        order = self._base_allegro_order(
            title="InPost Paczkomat (WAW10A)",
            method_id="c50d09e8-3b32-4e7a-8f4c-11a2b3c4d5e6",
            pickup_id="WAW10A",
        )
        _create_draft(order, store, storage, source="allegro")

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
        from zdrovena.api.routers.webhooks import _create_draft

        storage = object()
        order = self._base_allegro_order(
            title="InPost Kurier", method_id="aa11bb22-cc33-dd44-ee55-ff6677889900"
        )
        _create_draft(order, store, storage, source="allegro")

        d = store.list_drafts()[0]
        assert d["courier"] == "allegro_delivery"
        assert d["allegro_sending_method"] is None

    def test_allegro_inpost_kurier_env_flag_sets_dispatch_order(self, store, monkeypatch):
        """Za flagą operatora — można włączyć domyślny sending_method dla InPost Kurier."""
        monkeypatch.setenv("ALLEGRO_INPOST_KURIER_DEFAULT", "dispatch_order")
        from zdrovena.api.routers.webhooks import _create_draft

        storage = object()
        order = self._base_allegro_order(
            title="InPost Kurier", method_id="aa11bb22-cc33-dd44-ee55-ff6677889900"
        )
        _create_draft(order, store, storage, source="allegro")

        d = store.list_drafts()[0]
        assert d["allegro_sending_method"] == "dispatch_order"

    def test_allegro_non_inpost_no_sending_method(self, store):
        from zdrovena.api.routers.webhooks import _create_draft

        storage = object()
        order = self._base_allegro_order(
            title="Kurier DPD", method_id="11111111-2222-3333-4444-555555555555"
        )
        _create_draft(order, store, storage, source="allegro")

        d = store.list_drafts()[0]
        assert d["courier"] == "allegro_delivery"
        assert d["allegro_sending_method"] is None

    def test_allegro_without_method_id_fallback_to_apaczka(self, store):
        from zdrovena.api.routers.webhooks import _create_draft

        storage = object()
        order = self._base_allegro_order(title="Kurier DPD", method_id="")
        _create_draft(order, store, storage, source="allegro")

        d = store.list_drafts()[0]
        assert d["courier"] == "apaczka"
        assert d["service"] == "apaczka"

    def test_shopify_source_ignores_allegro_method_id(self, store):
        from zdrovena.api.routers.webhooks import _create_draft

        storage = object()
        order = self._base_allegro_order(
            title="InPost Paczkomat (WAW10A)", method_id="c50d09e8-3b32"
        )
        _create_draft(order, store, storage, source="shopify")

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
        from zdrovena.api.routers.webhooks import _create_draft

        storage = object()
        order = _load_fixture("shopify_order_inpost_paczkomat.json")
        _create_draft(order, store, storage)
        drafts = store.list_drafts()
        assert len(drafts) == 1
        d = drafts[0]
        assert d["service"] == "inpost_locker_standard"
        assert d["status"] == "pending"
        assert d["shopify_order_number"] == "1001"
        assert d["receiver"]["first_name"] == "Anna"
        assert d["receiver"]["last_name"] == "Kowalska"
        assert d["receiver"]["locker_id"] == "WAW123A"
        assert d["shipping_address"]["city"] == "Warszawa"
        assert d["tracking_number"] is None
        assert d["courier_draft_id"] is None


class TestCreateDraftApaczka:
    def test_apaczka_draft_stored(self, store):
        from zdrovena.api.routers.webhooks import _create_draft

        storage = object()
        order = _load_fixture("shopify_order_apaczka.json")
        _create_draft(order, store, storage)
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

    def test_apaczka_service_id_set_from_title_map(self, store, monkeypatch):
        from zdrovena.api.routers.webhooks import _create_draft, _reset_courier_maps_cache

        monkeypatch.setenv("APACZKA_SERVICE_TITLE_MAP", "dpd=21")
        _reset_courier_maps_cache()
        try:
            storage = object()
            order = _load_fixture("shopify_order_apaczka.json")
            _create_draft(order, store, storage)
            drafts = store.list_drafts()
            assert drafts[0]["apaczka_service_id"] == "21"
        finally:
            monkeypatch.delenv("APACZKA_SERVICE_TITLE_MAP", raising=False)
            _reset_courier_maps_cache()

    def test_apaczka_service_id_none_forces_needs_review(self, store, monkeypatch):
        """Fixture's shipping_lines[0].title is 'Apaczka DPD' — with no env
        mapping configured, apaczka_service_id stays unset and the draft must
        be needs_review even if phone/packages_count would otherwise pass."""
        from zdrovena.api.routers.webhooks import _create_draft, _reset_courier_maps_cache

        monkeypatch.delenv("APACZKA_SERVICE_TITLE_MAP", raising=False)
        _reset_courier_maps_cache()
        try:
            order = _load_fixture("shopify_order_apaczka.json")
            order["shipping_address"]["phone"] = "500600700"
            order["customer"]["phone"] = "500600700"
            storage = object()
            _create_draft(order, store, storage)
            drafts = store.list_drafts()
            assert drafts[0]["apaczka_service_id"] is None
            assert drafts[0]["status"] == "needs_review"
        finally:
            monkeypatch.delenv("APACZKA_SERVICE_TITLE_MAP", raising=False)
            _reset_courier_maps_cache()

    def test_apaczka_service_id_matched_allows_pending(self, store, monkeypatch):
        """Same phone fix as above, but WITH a matching title map — status
        should be 'pending', proving apaczka_service_id was the only blocker."""
        from zdrovena.api.routers.webhooks import _create_draft, _reset_courier_maps_cache

        monkeypatch.setenv("APACZKA_SERVICE_TITLE_MAP", "dpd=21")
        _reset_courier_maps_cache()
        try:
            order = _load_fixture("shopify_order_apaczka.json")
            order["shipping_address"]["phone"] = "500600700"
            order["customer"]["phone"] = "500600700"
            storage = object()
            _create_draft(order, store, storage)
            drafts = store.list_drafts()
            assert drafts[0]["apaczka_service_id"] == "21"
            assert drafts[0]["status"] == "pending"
        finally:
            monkeypatch.delenv("APACZKA_SERVICE_TITLE_MAP", raising=False)
            _reset_courier_maps_cache()

    def test_non_apaczka_draft_has_none_apaczka_service_id(self, store):
        """InPost/Allegro drafts get apaczka_service_id=None, never validated."""
        from zdrovena.api.routers.webhooks import _create_draft

        storage = object()
        order = _load_fixture("shopify_order_inpost_kurier.json")
        _create_draft(order, store, storage)
        drafts = store.list_drafts()
        assert drafts[0]["apaczka_service_id"] is None


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
        from zdrovena.api.routers.webhooks import _create_draft

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
            _create_draft(order, store, storage)
            mock_api.assert_not_called()
        drafts = store.list_drafts()
        assert len(drafts) == 1
        d = drafts[0]
        assert d["status"] == "pending"
        assert d["packages_count"] == 1
        assert d["courier_draft_id"] is None


class TestCreateDraftKaucjaFilter:
    def test_kaucja_excluded_from_packages_and_order_items(self, store):
        from zdrovena.api.routers.webhooks import _create_draft

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
        _create_draft(order, store, storage)
        drafts = store.list_drafts()
        assert len(drafts) == 1
        d = drafts[0]
        assert d["packages_count"] == 1
        item_names = [i["name"] for i in d["order_items"]]
        assert all("kaucja" not in n.lower() for n in item_names)
        assert len(d["order_items"]) == 1


class TestCalcPackages:
    """Unit tests for the _calc_packages packaging algorithm."""

    def _items(self, *specs):
        """Build product_items list from (name, qty) tuples."""
        return [{"name": n, "quantity": q} for n, q in specs]

    def _run(self, *specs):
        from zdrovena.api.routers.webhooks import _calc_packages

        items = self._items(*specs)
        count, breakdown = _calc_packages(items)
        bd = {b["type"]: b["qty"] for b in breakdown}
        return count, bd

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

    # ── Integracja z _create_draft ────────────────────────────────────────────

    def test_packages_breakdown_stored_in_draft(self, store):
        from zdrovena.api.routers.webhooks import _create_draft

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
        _create_draft(order, store, object())
        d = store.list_drafts()[0]
        assert d["packages_count"] == 2
        bd = {b["type"]: b["qty"] for b in d["packages_breakdown"]}
        assert bd == {"3-pak": 1, "2-pak": 1}


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
