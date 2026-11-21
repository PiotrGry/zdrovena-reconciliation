"""HTTP edge of the damage router: status mapping, validation and refresh.

Complements tests/test_damage_endpoints.py (happy-path workflow and crash-safe
email send) with the branches that translate domain refusals into status codes.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("AZURE_AUTH_DISABLED", "true")

from tests.test_damage_endpoints import _seed_case_and_draft, client, stores  # noqa: F401
from zdrovena.api import shipping_execution_composition
from zdrovena.shipping.application.execution import workflow as execution_workflow

CASE = "/api/damage-cases/case-1648"
MISSING = "/api/damage-cases/does-not-exist"


def _case_status(stores, status, **extra):  # noqa: F811
    stores[0].update_case("case-1648", {"status": status, **extra})


def _prepared(client, stores):  # noqa: F811
    _seed_case_and_draft(stores)
    client.post(f"{CASE}/confirm", json={})
    return client.post(f"{CASE}/prepare-replacement").json()["draft"]["id"]


# ── read endpoints ────────────────────────────────────────────────────────────


class TestReadEndpoints:
    def test_get_case_returns_it(self, client, stores):  # noqa: F811
        _seed_case_and_draft(stores)

        response = client.get(CASE)

        assert response.status_code == 200
        assert response.json()["id"] == "case-1648"
        assert response.json()["status"] == "needs_review"

    def test_get_missing_case_is_404(self, client):  # noqa: F811
        response = client.get(MISSING)

        assert response.status_code == 404
        assert response.json()["detail"] == "Damage case not found"

    def test_list_counts_needs_review_among_damage_cases(self, client, stores):  # noqa: F811
        _seed_case_and_draft(stores)
        stores[0].upsert_case({"id": "case-2", "status": "approved", "classification": "damage"})

        body = client.get("/api/damage-cases").json()

        assert {case["id"] for case in body["cases"]} == {"case-1648", "case-2"}
        assert body["needs_review"] == 1


# ── refresh ───────────────────────────────────────────────────────────────────


class TestRefresh:
    def test_nothing_configured_reports_both_as_skipped(self, client):  # noqa: F811
        with (
            patch.object(shipping_execution_composition, "get_allegro_client", return_value=None),
            patch("zdrovena.api.routers.damage.build_zoho_client", return_value=None),
        ):
            response = client.post("/api/damage-cases/refresh")

        assert response.status_code == 200
        body = response.json()
        assert body["allegro"] == {"skipped": "not_configured"}
        assert body["zoho"] == {"skipped": "not_configured"}
        assert body["needs_review"] == 0

    def test_both_providers_are_scanned(self, client, stores):  # noqa: F811
        _seed_case_and_draft(stores)
        allegro, zoho = MagicMock(), MagicMock()
        with (
            patch.object(
                shipping_execution_composition, "get_allegro_client", return_value=allegro
            ),
            patch("zdrovena.api.routers.damage.build_zoho_client", return_value=zoho),
            patch("zdrovena.api.routers.damage.build_inpost_lookup_client", return_value=None),
            patch("zdrovena.api.routers.damage.build_apaczka_lookup_client", return_value=None),
            patch(
                "zdrovena.api.routers.damage.scan_allegro_damage_cases",
                return_value={"scanned": 3, "created": 1},
            ) as scan_allegro,
            patch(
                "zdrovena.api.routers.damage.scan_zoho_damage_cases",
                return_value={"scanned": 5, "created": 0},
            ) as scan_zoho,
        ):
            response = client.post("/api/damage-cases/refresh")

        assert response.status_code == 200
        body = response.json()
        assert body["allegro"] == {"scanned": 3, "created": 1}
        assert body["zoho"] == {"scanned": 5, "created": 0}
        assert body["needs_review"] == 1
        assert scan_allegro.call_args.kwargs["client"] is allegro
        assert scan_zoho.call_args.kwargs["client"] is zoho

    def test_one_provider_failing_does_not_hide_the_other(self, client):  # noqa: F811
        with (
            patch.object(
                shipping_execution_composition,
                "get_allegro_client",
                side_effect=RuntimeError("allegro down"),
            ),
            patch("zdrovena.api.routers.damage.build_zoho_client", return_value=MagicMock()),
            patch("zdrovena.api.routers.damage.build_inpost_lookup_client", return_value=None),
            patch("zdrovena.api.routers.damage.build_apaczka_lookup_client", return_value=None),
            patch(
                "zdrovena.api.routers.damage.scan_zoho_damage_cases",
                return_value={"scanned": 2},
            ),
        ):
            response = client.post("/api/damage-cases/refresh")

        assert response.status_code == 200
        body = response.json()
        assert body["allegro"] == {"error": "allegro down"}
        assert body["zoho"] == {"scanned": 2}

    def test_zoho_failure_is_reported_as_error(self, client):  # noqa: F811
        with (
            patch.object(shipping_execution_composition, "get_allegro_client", return_value=None),
            patch(
                "zdrovena.api.routers.damage.build_zoho_client",
                side_effect=RuntimeError("zoho down"),
            ),
        ):
            response = client.post("/api/damage-cases/refresh")

        assert response.status_code == 200
        assert response.json()["zoho"] == {"error": "zoho down"}
        assert response.json()["allegro"] == {"skipped": "not_configured"}


# ── state transitions and domain refusals ─────────────────────────────────────


@pytest.fixture()
def no_zoho():
    with patch("zdrovena.api.routers.damage.build_zoho_client", return_value=None):
        yield


@pytest.mark.usefixtures("no_zoho")
class TestMissingCaseIs404:
    @pytest.mark.parametrize(
        ("method", "suffix", "payload"),
        [
            ("post", "confirm", {}),
            ("post", "ignore", None),
            ("post", "prepare-replacement", None),
            ("post", "create-replacement", None),
            ("post", "confirm-replacement", None),
            ("post", "email-draft", None),
            ("patch", "email-draft", {"subject": "s", "body": "b"}),
            ("post", "send-email", None),
            ("post", "resolve-email-attempt", {"delivered": True}),
            ("post", "close", None),
        ],
    )
    def test_every_action_on_a_missing_case_is_404(
        self,
        client,  # noqa: F811
        method,
        suffix,
        payload,
    ):
        kwargs = {"json": payload} if payload is not None else {}
        response = getattr(client, method)(f"{MISSING}/{suffix}", **kwargs)

        assert response.status_code == 404
        assert "does-not-exist" in response.json()["detail"]


@pytest.mark.usefixtures("no_zoho")
class TestInvalidTransitionsAre409:
    def test_confirm_twice_is_409(self, client, stores):  # noqa: F811
        _seed_case_and_draft(stores)
        assert client.post(f"{CASE}/confirm", json={"note": "ok"}).status_code == 200

        response = client.post(f"{CASE}/confirm", json={})

        assert response.status_code == 409
        assert response.json()["detail"] == "Case is not waiting for review"

    def test_confirm_stores_the_operator_note(self, client, stores):  # noqa: F811
        _seed_case_and_draft(stores)

        response = client.post(f"{CASE}/confirm", json={"note": "rozbita butelka"})

        assert response.status_code == 200
        assert response.json()["operator_note"] == "rozbita butelka"

    def test_confirm_rejects_an_overlong_note(self, client, stores):  # noqa: F811
        _seed_case_and_draft(stores)

        response = client.post(f"{CASE}/confirm", json={"note": "x" * 1001})

        assert response.status_code == 422
        assert stores[0].get_case("case-1648")["status"] == "needs_review"

    def test_ignore_a_fresh_case(self, client, stores):  # noqa: F811
        _seed_case_and_draft(stores)

        response = client.post(f"{CASE}/ignore")

        assert response.status_code == 200
        assert response.json()["status"] == "ignored"
        assert client.get("/api/damage-cases/summary").json() == {"needs_review": 0}

    def test_ignore_after_replacement_started_is_409(self, client, stores):  # noqa: F811
        _seed_case_and_draft(stores)
        _case_status(stores, "replacement_created")

        response = client.post(f"{CASE}/ignore")

        assert response.status_code == 409
        assert "already started" in response.json()["detail"]

    def test_prepare_before_confirm_is_409(self, client, stores):  # noqa: F811
        _seed_case_and_draft(stores)

        response = client.post(f"{CASE}/prepare-replacement")

        assert response.status_code == 409
        assert response.json()["detail"] == "Confirm damage before preparing replacement"

    def test_prepare_is_idempotent(self, client, stores):  # noqa: F811
        draft_id = _prepared(client, stores)

        again = client.post(f"{CASE}/prepare-replacement")

        assert again.status_code == 200
        assert again.json()["draft"]["id"] == draft_id
        assert again.json()["created"] is False

    def test_prepare_without_a_correlated_draft_is_409(self, client, stores):  # noqa: F811
        stores[0].upsert_case(
            {
                "id": "case-1648",
                "status": "approved",
                "classification": "damage",
                "tracking_number": "UNKNOWN-TRACKING",
            }
        )

        response = client.post(f"{CASE}/prepare-replacement")

        assert response.status_code == 409
        assert "correlate" in response.json()["detail"]

    def test_create_before_prepare_is_409(self, client, stores):  # noqa: F811
        _seed_case_and_draft(stores)

        response = client.post(f"{CASE}/create-replacement")

        assert response.status_code == 409
        assert response.json()["detail"] == "Prepare the replacement draft first"

    def test_confirm_replacement_without_draft_is_409(self, client, stores):  # noqa: F811
        _seed_case_and_draft(stores)

        response = client.post(f"{CASE}/confirm-replacement")

        assert response.status_code == 409
        assert response.json()["detail"] == "Replacement draft is missing"

    def test_email_draft_before_replacement_is_409(self, client, stores):  # noqa: F811
        _seed_case_and_draft(stores)

        response = client.post(f"{CASE}/email-draft")

        assert response.status_code == 409
        assert response.json()["detail"] == "Create the replacement parcel first"

    def test_editing_a_missing_email_draft_is_409(self, client, stores):  # noqa: F811
        _seed_case_and_draft(stores)

        response = client.patch(f"{CASE}/email-draft", json={"subject": "s", "body": "b"})

        assert response.status_code == 409
        assert response.json()["detail"] == "Prepare the email draft first"

    @pytest.mark.parametrize(
        "payload",
        [
            {"subject": "", "body": "b"},
            {"subject": "s", "body": ""},
            {"subject": "s" * 301, "body": "b"},
            {"subject": "s"},
        ],
    )
    def test_email_draft_edit_is_validated(self, client, stores, payload):  # noqa: F811
        _seed_case_and_draft(stores)

        response = client.patch(f"{CASE}/email-draft", json=payload)

        assert response.status_code == 422

    def test_send_without_draft_is_409(self, client, stores):  # noqa: F811
        _seed_case_and_draft(stores)

        response = client.post(f"{CASE}/send-email")

        assert response.status_code == 409
        assert response.json()["detail"] == "Prepare and review the email draft first"

    def test_send_without_zoho_is_503(self, client, stores):  # noqa: F811
        _seed_case_and_draft(stores)
        _case_status(
            stores,
            "replacement_created",
            email_draft={"from": "info@wodahumio.pl", "to": "a@b.c", "subject": "s", "body": "b"},
        )

        response = client.post(f"{CASE}/send-email")

        assert response.status_code == 503
        assert response.json()["detail"] == "Zoho Mail is not configured"

    def test_resolve_requires_delivered_flag(self, client, stores):  # noqa: F811
        _seed_case_and_draft(stores)

        response = client.post(f"{CASE}/resolve-email-attempt", json={})

        assert response.status_code == 422

    def test_close_a_fresh_case_is_409(self, client, stores):  # noqa: F811
        _seed_case_and_draft(stores)

        response = client.post(f"{CASE}/close")

        assert response.status_code == 409
        assert response.json()["detail"] == "Case is not ready to close"

    def test_close_after_customer_notified(self, client, stores):  # noqa: F811
        _seed_case_and_draft(stores)
        _case_status(stores, "customer_notified")

        response = client.post(f"{CASE}/close")

        assert response.status_code == 200
        assert response.json()["status"] == "closed"
        assert stores[0].get_case("case-1648")["status"] == "closed"


# ── shipment executor adapter ─────────────────────────────────────────────────


@pytest.mark.usefixtures("no_zoho")
class TestReplacementShipment:
    def test_executor_draft_not_found_is_404(self, client, stores):  # noqa: F811
        _prepared(client, stores)
        with patch.object(
            shipping_execution_composition,
            "execute_shipping_draft",
            side_effect=execution_workflow.DraftNotFoundError(),
        ):
            response = client.post(f"{CASE}/create-replacement")

        assert response.status_code == 404
        assert response.json()["detail"] == "Draft not found"
        assert stores[0].get_case("case-1648")["status"] == "replacement_prepared"

    def test_already_created_draft_is_recorded_not_reshipped(self, client, stores):  # noqa: F811
        draft_id = _prepared(client, stores)
        stores[1].update_draft(draft_id, {"status": "created", "tracking_number": "NEW-1"})
        with patch.object(shipping_execution_composition, "execute_shipping_draft") as execute:
            response = client.post(f"{CASE}/create-replacement")

        assert response.status_code == 200
        assert response.json()["case"]["status"] == "replacement_created"
        assert response.json()["case"]["replacement_tracking_number"] == "NEW-1"
        execute.assert_not_called()

    def test_vanished_replacement_draft_is_409(self, client, stores):  # noqa: F811
        _prepared(client, stores)
        _case_status(stores, "replacement_prepared", replacement_draft_id="gone")

        response = client.post(f"{CASE}/create-replacement")

        assert response.status_code == 409
        assert response.json()["detail"] == "Replacement draft no longer exists"

    def test_pending_shipment_is_recorded_as_pending(self, client, stores):  # noqa: F811
        draft_id = _prepared(client, stores)
        pending = {"id": draft_id, "status": "pending", "tracking_number": None}
        with patch.object(
            shipping_execution_composition, "execute_shipping_draft", return_value=pending
        ):
            response = client.post(f"{CASE}/create-replacement")

        assert response.status_code == 200
        assert response.json()["case"]["status"] == "replacement_pending"

    def test_confirm_replacement_records_created_shipment(self, client, stores):  # noqa: F811
        draft_id = _prepared(client, stores)
        result = shipping_execution_composition.ConfirmationResult(
            payload={"id": draft_id, "status": "created", "tracking_number": "A0052CONF"}
        )
        with patch.object(
            shipping_execution_composition, "confirm_shipping_draft", return_value=result
        ):
            response = client.post(f"{CASE}/confirm-replacement")

        assert response.status_code == 200
        body = response.json()
        assert body["case"]["status"] == "replacement_created"
        assert body["case"]["replacement_tracking_number"] == "A0052CONF"

    def test_confirm_replacement_error_keeps_its_status(self, client, stores):  # noqa: F811
        _prepared(client, stores)
        error = shipping_execution_composition.ConfirmationError(
            425, "Shipment still being processed"
        )
        with patch.object(
            shipping_execution_composition, "confirm_shipping_draft", side_effect=error
        ):
            response = client.post(f"{CASE}/confirm-replacement")

        assert response.status_code == 425
        assert response.json()["detail"] == "Shipment still being processed"


# ── SMTP configuration ────────────────────────────────────────────────────────


def test_missing_smtp_password_is_502_and_nothing_is_sent(client, stores):  # noqa: F811
    _seed_case_and_draft(stores)
    _case_status(
        stores,
        "replacement_created",
        email_draft={
            "from": "info@wodahumio.pl",
            "to": "jan@example.com",
            "subject": "s",
            "body": "b",
        },
    )
    zoho = MagicMock()
    zoho.sender_addresses.return_value = {"info@wodahumio.pl"}
    with (
        patch("zdrovena.api.routers.damage.build_zoho_client", return_value=zoho),
        patch("zdrovena.api.routers.damage.get_secret", return_value=None),
        patch("zdrovena.api.routers.damage.EmailService") as email_service,
    ):
        response = client.post(f"{CASE}/send-email")

    assert response.status_code == 502
    email_service.assert_not_called()
    case = stores[0].get_case("case-1648")
    assert "SMTP password" in case["email_error"]
    assert case.get("email_sent_at") is None
