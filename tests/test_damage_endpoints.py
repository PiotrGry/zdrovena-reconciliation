from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("AZURE_AUTH_DISABLED", "true")

from zdrovena.api.main import app
from zdrovena.api.routers import damage as damage_router
from zdrovena.api.routers import webhooks as shipping_webhooks
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


def test_replacement_clears_full_shipping_lifecycle_and_creates_new_parcels(stores):
    _seed_case_and_draft(stores)
    damage, shipping, storage = stores
    shipping.update_draft(
        "original",
        {
            "courier": "inpost",
            "service": "inpost_courier_standard",
            "packages_breakdown": [{"type": "1-pak", "qty": 2}],
            "courier_draft_id": "old-ship-1",
            "courier_shipments": [
                {
                    "id": "old-ship-1",
                    "tracking_number": "OLD-A",
                    "package_type": "1-pak",
                    "package_number": "1",
                },
                {
                    "id": "old-ship-2",
                    "tracking_number": "OLD-B",
                    "package_type": "1-pak",
                    "package_number": "2",
                },
            ],
            "allegro_shipment_id": "old-allegro-shipment",
            "allegro_dispatch_id": "old-allegro-pickup",
            "allegro_pickup_command_id": "old-allegro-pickup-command",
            "allegro_command_id": "old-allegro-command",
            "dispatch_order_id": "old-dispatch",
            "pickup_ordered": True,
            "shipment_origin": "system",
            "fulfillment_status": "fulfilled",
            "fulfilled_by": "operator@example.test",
            "fulfilled_at": "2026-07-14T09:00:00Z",
            "shopify_fulfillment_id": "fulfillment-1",
            "allegro_fulfillment_status": "SENT",
            "allegro_marked_processed_at": "2026-07-14T09:00:00Z",
            "allegro_marked_processed_by": "operator@example.test",
        },
    )

    original = shipping.get_draft("original")
    case = damage.get_case("case-1648")
    assert original is not None
    assert case is not None
    replacement = damage_router._clone_replacement_draft(original, case)

    for field in (
        "courier_draft_id",
        "courier_shipments",
        "allegro_shipment_id",
        "allegro_dispatch_id",
        "allegro_pickup_command_id",
        "allegro_command_id",
        "dispatch_order_id",
        "tracking_number",
        "shipment_origin",
        "fulfillment_status",
        "fulfilled_by",
        "fulfilled_at",
        "shopify_fulfillment_id",
        "allegro_fulfillment_status",
        "allegro_marked_processed_at",
        "allegro_marked_processed_by",
    ):
        assert not replacement.get(field), f"replacement retained {field}"
    assert replacement["pickup_ordered"] is False
    replacement["status"] = "pending"
    shipping.upsert_draft(replacement)

    with (
        patch("zdrovena.api.routers.webhooks.get_secret", return_value="test-value"),
        patch(
            "zdrovena.common.inpost.InPostClient.create_kurier_shipment",
            side_effect=[
                {"id": "new-ship-1", "tracking_number": "NEW-A"},
                {"id": "new-ship-2", "tracking_number": "NEW-B"},
            ],
        ) as create_shipment,
    ):
        created = shipping_webhooks.execution_workflow.execute_draft(
            replacement["id"],
            shipping,
            **shipping_webhooks._execution_collaborators(storage),
        )

    assert created["status"] == "created"
    assert create_shipment.call_count == 2
    saved = shipping.get_draft(replacement["id"])
    assert [item["id"] for item in saved["courier_shipments"]] == [
        "new-ship-1",
        "new-ship-2",
    ]
    assert not ({"old-ship-1", "old-ship-2"} & {item["id"] for item in saved["courier_shipments"]})
    assert saved.get("fulfillment_status") is None
    assert saved.get("fulfilled_by") is None


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

    with patch(
        "zdrovena.shipping.application.execution.workflow.execute_draft",
        side_effect=execute_side_effect,
    ):
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
