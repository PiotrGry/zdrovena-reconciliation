from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("AZURE_AUTH_DISABLED", "true")

from zdrovena.api.main import app
from zdrovena.common.damage_store import DamageStore
from zdrovena.common.shipping_store import ShippingStore
from zdrovena.common.storage import LocalStorageService


@pytest.fixture()
def stores(tmp_path):
    return (
        DamageStore(local_root=tmp_path / "damage"),
        ShippingStore(local_root=tmp_path / "shipping"),
        LocalStorageService(root=tmp_path / "files"),
    )


@pytest.fixture()
def client(stores):
    damage, shipping, storage = stores
    with (
        patch("zdrovena.api.deps._damage_store_singleton", return_value=damage),
        patch("zdrovena.api.deps._shipping_store_singleton", return_value=shipping),
        patch("zdrovena.api.deps._storage_singleton", return_value=storage),
        TestClient(app, raise_server_exceptions=True) as test_client,
    ):
        yield test_client


def _seed_case_and_draft(stores):
    damage, shipping, _storage = stores
    shipping.upsert_draft(
        {
            "id": "original",
            "created_at": "2026-07-14T08:00:00Z",
            "source": "allegro",
            "external_order_id": "order-1648",
            "shopify_order_id": None,
            "shopify_order_number": "1648",
            "customer_name": "Jan Kowalski",
            "receiver": {
                "first_name": "Jan",
                "last_name": "Kowalski",
                "email": "jan@example.com",
                "phone": "48123456789",
            },
            "shipping_address": {},
            "parcel": {"template": "large"},
            "courier": "allegro_delivery",
            "service": "allegro_delivery",
            "status": "created",
            "tracking_number": "A0052HFZF6",
            "fakturownia_invoice_id": "invoice-1",
        }
    )
    damage.upsert_case(
        {
            "id": "case-1648",
            "created_at": "2026-07-15T13:40:42Z",
            "updated_at": "2026-07-15T13:40:42Z",
            "detected_at": "2026-07-15T13:40:42Z",
            "status": "needs_review",
            "classification": "damage",
            "tracking_number": "A0052HFZF6",
            "shipping_draft_id": "original",
            "order_number": "1648",
            "customer_email": "jan@example.com",
            "evidence": [],
        }
    )


def test_manual_workflow_prepares_separate_draft(client, stores):
    _seed_case_and_draft(stores)
    damage, shipping, _storage = stores

    assert client.get("/api/damage-cases/summary").json() == {"needs_review": 1}
    confirmed = client.post("/api/damage-cases/case-1648/confirm", json={})
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "approved"

    prepared = client.post("/api/damage-cases/case-1648/prepare-replacement")
    assert prepared.status_code == 200
    replacement = prepared.json()["draft"]
    assert replacement["id"] != "original"
    assert replacement["status"] == "needs_review"
    assert replacement["tracking_number"] is None
    assert replacement["fakturownia_invoice_id"] is None
    assert replacement["replacement_for_tracking_number"] == "A0052HFZF6"
    assert shipping.get_draft("original")["status"] == "created"
    assert damage.get_case("case-1648")["status"] == "replacement_prepared"


def test_damage_list_hides_legacy_non_damage_carrier_issue(client, stores):
    damage, _shipping, _storage = stores
    damage.upsert_case(
        {
            "id": "delay-only",
            "status": "needs_review",
            "classification": "carrier_issue",
            "tracking_number": "DELAY123",
        }
    )

    response = client.get("/api/damage-cases")

    assert response.status_code == 200
    assert response.json() == {"cases": [], "needs_review": 0}


def test_create_email_edit_and_send_are_separate_actions(client, stores):
    _seed_case_and_draft(stores)
    damage, shipping, _storage = stores
    client.post("/api/damage-cases/case-1648/confirm", json={})
    prepared = client.post("/api/damage-cases/case-1648/prepare-replacement").json()
    replacement_id = prepared["draft"]["id"]

    def execute_side_effect(draft_id, shipping_store, *_args, **_kwargs):
        assert draft_id == replacement_id
        shipping_store.update_draft(
            draft_id,
            {"status": "created", "tracking_number": "A0052NEW123"},
        )
        return shipping_store.get_draft(draft_id)

    with patch("zdrovena.api.routers.webhooks.execute_draft", side_effect=execute_side_effect):
        created = client.post("/api/damage-cases/case-1648/create-replacement")
    assert created.status_code == 200
    assert created.json()["case"]["status"] == "replacement_created"

    drafted = client.post("/api/damage-cases/case-1648/email-draft")
    assert drafted.status_code == 200
    email = drafted.json()["email_draft"]
    assert email["from"] == "info@wodahumio.pl"
    assert email["to"] == "jan@example.com"
    assert "A0052NEW123" in email["body"]

    edited = client.patch(
        "/api/damage-cases/case-1648/email-draft",
        json={"subject": "Nowa paczka", "body": "Sprawdzona treść"},
    )
    assert edited.status_code == 200

    zoho = MagicMock()
    zoho.sender_addresses.return_value = {"info@wodahumio.pl"}
    with (
        patch("zdrovena.api.routers.damage.build_zoho_client", return_value=zoho),
        patch("zdrovena.api.routers.damage.get_secret", return_value="smtp-password"),
        patch("zdrovena.api.routers.damage.EmailService") as email_service,
    ):
        sent = client.post("/api/damage-cases/case-1648/send-email")
    assert sent.status_code == 200
    assert sent.json()["case"]["status"] == "customer_notified"
    email_service.assert_called_once_with(
        smtp_password="smtp-password",
        sender_email="piotr@wodahumio.pl",
        from_email="info@wodahumio.pl",
    )
    email_service.return_value.send_report.assert_called_once_with(
        "jan@example.com", "Nowa paczka", "Sprawdzona treść"
    )
    assert damage.get_case("case-1648")["email_provider_message_id"] is None
    assert shipping.get_draft(replacement_id)["tracking_number"] == "A0052NEW123"


def test_send_rejects_unconfigured_info_alias(client, stores):
    _seed_case_and_draft(stores)
    damage, _shipping, _storage = stores
    damage.update_case(
        "case-1648",
        {
            "status": "replacement_created",
            "email_draft": {
                "from": "info@wodahumio.pl",
                "to": "jan@example.com",
                "subject": "Test",
                "body": "Test",
            },
        },
    )
    zoho = MagicMock()
    zoho.sender_addresses.return_value = {"piotr@wodahumio.pl"}
    with patch("zdrovena.api.routers.damage.build_zoho_client", return_value=zoho):
        response = client.post("/api/damage-cases/case-1648/send-email")
    assert response.status_code == 409
    assert "info@wodahumio.pl" in response.json()["detail"]
