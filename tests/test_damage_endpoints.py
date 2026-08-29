from __future__ import annotations

import os
import smtplib
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("AZURE_AUTH_DISABLED", "true")

from zdrovena.api import shipping_execution_composition
from zdrovena.api.main import app
from zdrovena.api.routers import damage as damage_router
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
        patch("zdrovena.api.shipping_execution_composition.get_secret", return_value="test-value"),
        patch(
            "zdrovena.common.inpost.InPostClient.create_kurier_shipment",
            side_effect=[
                {"id": "new-ship-1", "tracking_number": "NEW-A"},
                {"id": "new-ship-2", "tracking_number": "NEW-B"},
            ],
        ) as create_shipment,
    ):
        created = shipping_execution_composition.execute_shipping_draft(
            replacement["id"],
            shipping,
            storage,
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
        "zdrovena.api.shipping_execution_composition.execute_shipping_draft",
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


class TestStorageOutageSurfacesAs503:
    """Issue #310: the damage surface must answer an outage the same way the
    shipping and DLQ surfaces do — 503, not an empty list."""

    def test_an_outage_is_503_not_an_empty_list(self, client, stores):
        from zdrovena.common.exceptions import storage_unavailable

        def _boom(*args, **kwargs):
            raise storage_unavailable("damage", "list_cases", RuntimeError("timeout"))

        with patch.object(DamageStore, "list_cases", _boom):
            resp = client.get("/api/damage-cases")

        assert resp.status_code == 503
        body = resp.json()
        assert body["error_code"] == "StorageUnavailableError"
        assert body["correlation_id"]


class TestCrashSafeEmailSend:
    """Fault injection around an irreversible external effect (issue #312).

    An atomic claim stops two clicks from both sending. It does not close the
    window between "SMTP accepted the message" and "we managed to write that
    down" — and the old claim's ten-minute expiry is what turned that window
    into a second email.
    """

    def _ready_case(self, stores):
        _seed_case_and_draft(stores)
        damage, _shipping, _storage = stores
        damage.update_case(
            "case-1648",
            {
                "status": "replacement_created",
                "email_draft": {
                    "from": "info@wodahumio.pl",
                    "to": "jan@example.com",
                    "subject": "Nowa paczka",
                    "body": "Treść",
                },
            },
        )
        return damage

    def _send(self, client, *, smtp_side_effect=None):
        zoho = MagicMock()
        zoho.sender_addresses.return_value = {"info@wodahumio.pl"}
        service = MagicMock()
        if smtp_side_effect is not None:
            service.return_value.send_report.side_effect = smtp_side_effect
        with (
            patch("zdrovena.api.routers.damage.build_zoho_client", return_value=zoho),
            patch("zdrovena.api.routers.damage.get_secret", return_value="smtp-password"),
            patch("zdrovena.api.routers.damage.EmailService", service),
        ):
            return client.post("/api/damage-cases/case-1648/send-email"), service

    def test_the_attempt_is_durable_before_smtp_is_contacted(self, client, stores):
        """Otherwise a crash leaves an accepted message with nothing on disk."""
        damage = self._ready_case(stores)
        seen = {}

        def capture(*_args, **_kwargs):
            seen["attempt"] = damage.get_case("case-1648").get("email_attempt")

        self._send(client, smtp_side_effect=capture)

        assert seen["attempt"], "no attempt was recorded before the SMTP call"
        assert seen["attempt"]["state"] == "pending"
        assert len(seen["attempt"]["fingerprint"]) == 64

    def test_the_fingerprint_holds_no_personal_data(self, client, stores):
        self._ready_case(stores)
        self._send(client)

        attempt = stores[0].get_case("case-1648")["email_attempt"]

        assert "jan@example.com" not in str(attempt)

    def test_a_successful_send_confirms_the_attempt(self, client, stores):
        self._ready_case(stores)
        response, _service = self._send(client)

        assert response.status_code == 200
        assert stores[0].get_case("case-1648")["email_attempt"]["state"] == "confirmed"

    def test_a_refusal_we_saw_is_recorded_as_failed_and_retryable(self, client, stores):
        """Nothing was delivered, so a retry is safe."""
        damage = self._ready_case(stores)
        refusal = smtplib.SMTPResponseException(550, b"rejected")

        response, _service = self._send(client, smtp_side_effect=refusal)
        assert response.status_code == 502
        assert damage.get_case("case-1648")["email_attempt"]["state"] == "failed"

        retried, service = self._send(client)
        assert retried.status_code == 200
        service.return_value.send_report.assert_called_once()

    def test_a_timeout_is_not_recorded_as_a_clean_failure(self, client, stores):
        """We never saw a refusal, so the message may have been accepted.
        Recording `failed` here would invite an automatic second send."""
        damage = self._ready_case(stores)

        response, _service = self._send(client, smtp_side_effect=TimeoutError("no answer"))

        assert response.status_code == 502
        assert damage.get_case("case-1648")["email_attempt"]["state"] == "pending"

    def test_a_crash_after_smtp_accepted_does_not_send_again(self, client, stores):
        """The scenario this issue exists for.

        Simulates the process dying between SMTP accepting and the final write,
        then the claim ageing past its old ten-minute expiry.
        """
        damage = self._ready_case(stores)
        stranded = {
            "id": "attempt-1",
            "state": "pending",
            "fingerprint": "f" * 64,
            "started_at": "2020-01-01T00:00:00+00:00",
            "settled_at": None,
        }
        damage.update_case("case-1648", {"email_attempt": stranded, "email_sending": True})

        response, service = self._send(client)

        assert response.status_code == 409
        service.return_value.send_report.assert_not_called()
        assert "jednoznacz" in response.json()["detail"]

    def test_the_operator_can_record_that_it_was_delivered(self, client, stores):
        damage = self._ready_case(stores)
        damage.update_case(
            "case-1648",
            {
                "email_attempt": {
                    "id": "attempt-1",
                    "state": "unknown",
                    "fingerprint": "f" * 64,
                    "started_at": "2020-01-01T00:00:00+00:00",
                }
            },
        )

        resolved = client.post(
            "/api/damage-cases/case-1648/resolve-email-attempt",
            json={"delivered": True, "note": "sprawdzone w Zoho"},
        )

        assert resolved.status_code == 200
        case = damage.get_case("case-1648")
        assert case["email_attempt"]["state"] == "confirmed"
        assert case["email_sent_at"]
        assert case["status"] == "customer_notified"

    def test_recording_non_delivery_unblocks_a_retry(self, client, stores):
        damage = self._ready_case(stores)
        damage.update_case(
            "case-1648",
            {
                "email_attempt": {
                    "id": "attempt-1",
                    "state": "unknown",
                    "fingerprint": "f" * 64,
                    "started_at": "2020-01-01T00:00:00+00:00",
                }
            },
        )

        assert (
            client.post(
                "/api/damage-cases/case-1648/resolve-email-attempt",
                json={"delivered": False},
            ).status_code
            == 200
        )

        retried, service = self._send(client)
        assert retried.status_code == 200
        service.return_value.send_report.assert_called_once()

    def test_there_is_nothing_to_resolve_on_a_clean_case(self, client, stores):
        self._ready_case(stores)

        response = client.post(
            "/api/damage-cases/case-1648/resolve-email-attempt", json={"delivered": True}
        )

        assert response.status_code == 409
