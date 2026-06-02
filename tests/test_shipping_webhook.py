"""Tests for zdrovena.api.routers.webhooks — HMAC validation, courier routing, and endpoints."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("AZURE_AUTH_DISABLED", "true")

from zdrovena.api.main import app
from zdrovena.api.routers.webhooks import _pick_courier, _verify_shopify_hmac
from zdrovena.common.shipping_store import ShippingStore

_FIXTURES = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> dict:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


# ── Pure helpers ──────────────────────────────────────────────────────────────


def _sign(body: bytes, secret: str) -> str:
    return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


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
def client(tmp_path, store):
    from zdrovena.common.storage import LocalStorageService

    storage = LocalStorageService(root=tmp_path / "storage")
    with patch("zdrovena.api.deps._storage_singleton", return_value=storage):
        with patch("zdrovena.api.deps._shipping_store_singleton", return_value=store):
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
        with patch("zdrovena.api.routers.webhooks._get_webhook_secret", return_value=None):
            resp = client.post(
                "/api/webhooks/shopify/order-created",
                content=_ORDER_NO_SHIPPING,
                headers={"Content-Type": "application/json"},
            )
        assert resp.status_code == 200
        assert resp.json() == {"status": "skipped"}

    def test_no_secret_configured_skips_hmac(self, client):
        with patch("zdrovena.api.routers.webhooks._get_webhook_secret", return_value=None):
            with patch("zdrovena.api.routers.webhooks._create_draft"):
                resp = client.post(
                    "/api/webhooks/shopify/order-created",
                    content=_ORDER_WITH_SHIPPING,
                    headers={"Content-Type": "application/json"},
                )
        assert resp.status_code == 200
        assert resp.json() == {"status": "accepted"}

    def test_valid_hmac_accepted(self, client):
        secret = "test-webhook-secret"
        sig = _sign(_ORDER_WITH_SHIPPING, secret)
        with patch("zdrovena.api.routers.webhooks._get_webhook_secret", return_value=secret):
            with patch("zdrovena.api.routers.webhooks._create_draft"):
                resp = client.post(
                    "/api/webhooks/shopify/order-created",
                    content=_ORDER_WITH_SHIPPING,
                    headers={"Content-Type": "application/json", "X-Shopify-Hmac-Sha256": sig},
                )
        assert resp.status_code == 200
        assert resp.json() == {"status": "accepted"}

    def test_invalid_hmac_rejected(self, client):
        secret = "test-webhook-secret"
        with patch("zdrovena.api.routers.webhooks._get_webhook_secret", return_value=secret):
            resp = client.post(
                "/api/webhooks/shopify/order-created",
                content=_ORDER_WITH_SHIPPING,
                headers={"Content-Type": "application/json", "X-Shopify-Hmac-Sha256": "bad"},
            )
        assert resp.status_code == 401

    def test_missing_hmac_header_with_secret_configured_rejected(self, client):
        with patch("zdrovena.api.routers.webhooks._get_webhook_secret", return_value="secret"):
            resp = client.post(
                "/api/webhooks/shopify/order-created",
                content=_ORDER_WITH_SHIPPING,
                headers={"Content-Type": "application/json"},
            )
        assert resp.status_code == 401

    def test_invalid_json_returns_400(self, client):
        with patch("zdrovena.api.routers.webhooks._get_webhook_secret", return_value=None):
            resp = client.post(
                "/api/webhooks/shopify/order-created",
                content=b"not-json",
                headers={"Content-Type": "application/json"},
            )
        assert resp.status_code == 400


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

    def test_400_for_paczkomat_draft(self, client, store):
        draft = self._seed_created_kurier(store)
        store.update_draft(draft["id"], {"service": "inpost_locker_standard"})
        resp = client.post(f"/api/shipping/drafts/{draft['id']}/pickup")
        assert resp.status_code == 400

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
        assert d["packages_count"] == 1
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
        draft = self._seed_created_draft(store)
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
        assert d["status"] == "pending"
        assert d["tracking_number"] is None
        assert d["courier_draft_id"] is None
        assert d["shopify_order_number"] == "1003"
        assert d["receiver"]["first_name"] == "Maria"
        assert d["receiver"]["last_name"] == "Wiśniewska"
        assert d["receiver"]["email"] == "maria.wisniewska@example.com"
        assert d["shipping_address"]["city"] == "Gdańsk"


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
        with patch("zdrovena.api.routers.webhooks.get_secret", return_value="tok"):
            with patch(
                "zdrovena.common.apaczka.ApaczkaClient.get_label", return_value=b"%PDF-1.4 apaczka"
            ):
                resp = client.get(f"/api/shipping/drafts/{draft['id']}/label?courier=apaczka")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"


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
