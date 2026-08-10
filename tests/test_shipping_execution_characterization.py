"""Characterization tests for the shipment execution orchestration boundary."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("AZURE_AUTH_DISABLED", "true")

from zdrovena.api.main import app
from zdrovena.api.routers import webhooks
from zdrovena.common.shipping_exceptions import InPostBusinessError
from zdrovena.common.shipping_store import DLQ_KIND_EXECUTION, ShippingStore
from zdrovena.shipping.application.execution import fingerprint as execution_fingerprint
from zdrovena.shipping.application.execution import workflow as execution_workflow

_SENDER = {
    "name": "Zdrovena",
    "firstname": "",
    "lastname": "Zdrovena",
    "street": "Cieszyńska",
    "building_number": "6",
    "city": "Kraków",
    "post_code": "30-015",
    "phone": "+48123456789",
    "email": "shipping@example.test",
}


def _draft(
    draft_id: str = "execution-characterization",
    *,
    courier: str | None = "inpost",
    status: str = "error",
    **overrides: Any,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "id": draft_id,
        "created_at": "2026-08-09T12:00:00+00:00",
        "source": "shopify",
        "external_order_id": "external-1701",
        "shopify_order_id": "1701",
        "shopify_order_number": "1701",
        "courier": courier,
        "service": "inpost_courier_standard",
        "status": status,
        "tracking_number": None,
        "courier_draft_id": None,
        "courier_shipments": [],
        "packages_count": 1,
        "packages_breakdown": [{"type": "1-pak", "qty": 1}],
        "pickup_ordered": False,
        "receiver": {
            "first_name": "Anna",
            "last_name": "Nowak",
            "email": "anna@example.test",
            "phone": "+48500000000",
            "locker_id": "",
        },
        "shipping_address": {
            "street": "Testowa",
            "building_number": "7",
            "flat_number": "2",
            "city": "Warszawa",
            "post_code": "00-001",
        },
        "error": "previous failure",
    }
    record.update(overrides)
    return record


@pytest.fixture()
def store(tmp_path) -> ShippingStore:
    return ShippingStore(local_root=tmp_path / "shipping")


@pytest.fixture()
def client(store):
    with patch("zdrovena.api.deps._shipping_store_singleton", return_value=store):
        with TestClient(app, raise_server_exceptions=False) as test_client:
            yield test_client


def _execute_application(
    draft_id: str,
    repository: Any,
    storage: Any,
    **kwargs: Any,
) -> dict[str, Any]:
    return execution_workflow.execute_draft(
        draft_id,
        repository,
        **webhooks._execution_collaborators(storage),
        **kwargs,
    )


class TestExecutionFingerprintCharacterization:
    def test_exact_digest_and_json_serialization_options(self) -> None:
        fixed_draft = {
            "status": "error",
            "receiver": {"note": "zażółć", "city": "Łódź"},
            "id": "draft-ą",
            "courier": "inpost",
            "created_at": datetime(2026, 8, 9, 12, 34, 56, tzinfo=timezone.utc),
        }
        sender = {"name": "Żaneta", "city": "Łódź"}
        parcels = [
            {
                "service": "inpost_courier_standard",
                "package_type": "1-pak",
                "package_number": 1,
                "reference": "REF-1",
                "payload": {"z": 2, "a": "ą"},
            }
        ]
        fixed_preview = {
            "courier": "inpost",
            "sender": sender,
            "parcels": parcels,
            "preview_available": True,
        }
        dumps_kwargs: dict[str, Any] = {}
        real_dumps = json.dumps

        def recording_dumps(value: Any, **kwargs: Any) -> str:
            dumps_kwargs.update(kwargs)
            return real_dumps(value, **kwargs)

        with patch.object(
            execution_fingerprint.json,
            "dumps",
            side_effect=recording_dumps,
        ):
            fingerprint = execution_fingerprint.preview_fingerprint(fixed_draft, fixed_preview)

        assert fingerprint == ("0ddbe1c6c1519e5b38b63f1ec26b21db9c91184f51a9757412bd30098932bbf2")
        assert dumps_kwargs == {
            "sort_keys": True,
            "separators": (",", ":"),
            "ensure_ascii": False,
            "default": str,
        }

    def test_webhooks_fingerprint_wrappers_delegate_to_application_helpers(self) -> None:
        draft = {"id": "compat-draft"}
        preview = {"courier": "inpost", "parcels": []}

        with (
            patch.object(
                execution_fingerprint,
                "preview_fingerprint",
                return_value="compatibility-digest",
            ) as calculate,
            patch.object(
                execution_fingerprint,
                "fingerprints_match",
                return_value=True,
            ) as compare,
        ):
            result = webhooks._preview_fingerprint(draft, preview)
            matched = webhooks._fingerprints_match("current", "reviewed")

        assert result == "compatibility-digest"
        assert matched is True
        calculate.assert_called_once_with(draft, preview)
        compare.assert_called_once_with("current", "reviewed")

    def test_fingerprint_verification_uses_constant_time_compare_digest(self) -> None:
        with patch.object(
            execution_fingerprint.hmac,
            "compare_digest",
            return_value=True,
        ) as compare:
            matched = execution_fingerprint.fingerprints_match("current", "reviewed")

        assert matched is True
        compare.assert_called_once_with("current", "reviewed")

    def test_mismatch_is_rejected_before_execution_claim(self) -> None:
        draft = _draft()
        repository = MagicMock()
        repository.get_draft.return_value = draft

        with (
            patch.object(
                webhooks,
                "_execution_preview",
                return_value={"fingerprint": "current", "sender": _SENDER},
            ),
            patch.object(webhooks, "_run_inpost") as run_inpost,
            pytest.raises(execution_workflow.PreviewFingerprintMismatchError) as caught,
        ):
            _execute_application(
                draft["id"],
                repository,
                object(),
                preview_fingerprint="reviewed",
            )

        assert (
            str(caught.value) == "Draft changed after preview — review the courier payload again."
        )
        repository.try_claim_execution.assert_not_called()
        run_inpost.assert_not_called()

    def test_missing_fingerprint_is_accepted_and_preview_is_not_rebuilt(self, store) -> None:
        draft = _draft("no-fingerprint")
        store.upsert_draft(draft)
        provider_patch = {
            "courier_draft_id": "shipment-no-fingerprint",
            "tracking_number": "TRACK-NO-FINGERPRINT",
            "status": "created",
            "error": None,
        }

        with (
            patch.object(webhooks, "_execution_preview", side_effect=AssertionError),
            patch.object(webhooks, "_get_sender", return_value=_SENDER),
            patch.object(webhooks, "_run_inpost", return_value=provider_patch) as run_inpost,
        ):
            result = _execute_application(draft["id"], store, object())

        assert result["status"] == "created"
        run_inpost.assert_called_once()


class TestExecutionClaimCharacterization:
    def test_successful_claim_precedes_provider_execution(self) -> None:
        draft = _draft("claim-order")
        calls: list[str] = []
        repository = MagicMock()
        lookup_count = 0

        def get_draft(_draft_id: str) -> dict[str, Any]:
            nonlocal lookup_count
            lookup_count += 1
            return draft if lookup_count == 1 else {**draft, "status": "created"}

        def claim(_draft_id: str) -> bool:
            calls.append("claim")
            return True

        def run_provider(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            calls.append("provider")
            return {
                "courier_draft_id": "shipment-claim-order",
                "tracking_number": "TRACK-CLAIM",
                "status": "created",
                "error": None,
            }

        repository.get_draft.side_effect = get_draft
        repository.try_claim_execution.side_effect = claim

        with (
            patch.object(webhooks, "_get_sender", return_value=_SENDER),
            patch.object(webhooks, "_run_inpost", side_effect=run_provider),
            patch.object(webhooks, "log_event"),
            patch.object(webhooks, "_emit_tracking_assigned"),
            patch.object(webhooks, "_maybe_push_tracking_to_allegro"),
        ):
            _execute_application(draft["id"], repository, object())

        assert calls == ["claim", "provider"]
        repository.try_claim_execution.assert_called_once_with(draft["id"])

    def test_failed_claim_preserves_existing_conflict_and_skips_provider(self) -> None:
        draft = _draft("claim-lost")
        repository = MagicMock()
        repository.get_draft.return_value = draft
        repository.try_claim_execution.return_value = False

        with (
            patch.object(webhooks, "_run_inpost") as run_inpost,
            pytest.raises(execution_workflow.ExecutionClaimConflictError) as caught,
        ):
            _execute_application(draft["id"], repository, object())

        assert str(caught.value) == (
            "Draft already executed or in progress — nie realizuj ponownie."
        )
        run_inpost.assert_not_called()

    @pytest.mark.parametrize(
        ("current_status", "updates"),
        [("executing", 1), ("created", 0), ("pending_confirmation", 0)],
    )
    def test_failed_execution_release_only_updates_still_executing_state(
        self, current_status: str, updates: int
    ) -> None:
        repository = MagicMock()
        repository.get_draft.return_value = {"id": "release-claim", "status": current_status}

        execution_workflow.release_execution_claim(
            repository,
            "release-claim",
            "provider failed",
            log_exception=MagicMock(),
        )

        assert repository.update_draft.call_count == updates
        if updates:
            repository.update_draft.assert_called_once_with(
                "release-claim",
                {"status": "error", "error": "provider failed"},
            )


class TestPartialShipmentPersistenceCharacterization:
    def test_retry_keeps_first_parcel_and_only_creates_second(self, store) -> None:
        draft = _draft(
            "partial-two-parcels",
            packages_count=2,
            packages_breakdown=[{"type": "1-pak", "qty": 2}],
        )
        store.upsert_draft(draft)

        with (
            patch.object(webhooks, "_get_sender", return_value=_SENDER),
            patch.object(webhooks, "get_secret", return_value="token"),
            patch(
                "zdrovena.common.inpost.InPostClient.create_kurier_shipment",
                side_effect=[
                    {"id": "shipment-first", "tracking_number": "TRACK-1"},
                    RuntimeError("second parcel failed"),
                ],
            ) as first_attempt,
            pytest.raises(execution_workflow.ExecutionCommunicationError) as caught,
        ):
            _execute_application(draft["id"], store, object())

        assert str(caught.value.original) == "second parcel failed"
        assert first_attempt.call_count == 2
        after_failure = store.get_draft(draft["id"])
        assert after_failure is not None
        assert after_failure["status"] == "error"
        assert after_failure["courier_shipments"] == [
            {
                "id": "shipment-first",
                "tracking_number": "TRACK-1",
                "package_type": "1-pak",
                "package_number": "1",
            }
        ]

        with (
            patch.object(webhooks, "_get_sender", return_value=_SENDER),
            patch.object(webhooks, "get_secret", return_value="token"),
            patch(
                "zdrovena.common.inpost.InPostClient.create_kurier_shipment",
                return_value={"id": "shipment-second", "tracking_number": "TRACK-2"},
            ) as retry_create,
        ):
            result = _execute_application(draft["id"], store, object())

        retry_create.assert_called_once()
        assert retry_create.call_args.kwargs["reference"].endswith("2/2")
        assert [shipment["id"] for shipment in result["courier_shipments"]] == [
            "shipment-first",
            "shipment-second",
        ]


class TestExecutionFailureAndDlqCharacterization:
    def test_shipping_error_preserves_existing_api_mapping(self, client, store) -> None:
        draft = _draft("typed-provider-failure")
        store.upsert_draft(draft)
        provider_error = InPostBusinessError(
            "invalid parcel",
            order_id=draft["id"],
            courier="inpost",
            action="create_shipment",
        )

        with (
            patch.object(webhooks, "_get_sender", return_value=_SENDER),
            patch.object(webhooks, "_run_inpost", side_effect=provider_error),
        ):
            response = client.post(f"/api/shipping/drafts/{draft['id']}/execute")

        assert response.status_code == 422
        assert response.json() == {
            "error_code": "InPostBusinessError",
            "message_pl": "Przewoźnik odrzucił przesyłkę — wymagana reakcja operatora.",
            "details": {
                "courier": "inpost",
                "action": "create_shipment",
                "order_id": draft["id"],
            },
            "correlation_id": response.json()["correlation_id"],
        }
        assert store.get_draft(draft["id"])["status"] == "error"

    def test_generic_error_preserves_existing_502_response(self, client, store) -> None:
        draft = _draft("generic-provider-failure")
        store.upsert_draft(draft)

        with (
            patch.object(webhooks, "_get_sender", return_value=_SENDER),
            patch.object(webhooks, "_run_inpost", side_effect=RuntimeError("provider exploded")),
        ):
            response = client.post(f"/api/shipping/drafts/{draft['id']}/execute")

        assert response.status_code == 502
        assert response.json() == {
            "detail": "Błąd komunikacji z przewoźnikiem — spróbuj ponownie za chwilę."
        }
        failed = store.get_draft(draft["id"])
        assert failed["status"] == "error"
        assert failed["error"] == "provider exploded"

    def test_retry_failure_updates_the_same_dlq_entry_id(self, store) -> None:
        draft = _draft("stable-dlq-retry")
        store.upsert_draft(draft)
        entry = store.enqueue_dlq(
            payload=draft,
            error="RuntimeError: first failure",
            source="shopify",
            kind=DLQ_KIND_EXECUTION,
            draft_id=draft["id"],
            entry_id="stable-execution-entry",
        )

        with (
            patch.object(webhooks, "_get_sender", return_value=_SENDER),
            patch.object(webhooks, "_run_inpost", side_effect=RuntimeError("retry failed")),
            pytest.raises(execution_workflow.ExecutionCommunicationError),
        ):
            _execute_application(
                draft["id"],
                store,
                object(),
                failure_dlq_entry_id=entry["id"],
            )

        entries = store.list_dlq()
        assert [item["id"] for item in entries] == [entry["id"]]
        assert entries[0]["retries"] == 1
        assert entries[0]["last_error"] == "RuntimeError: retry failed"

    def test_dlq_write_failure_does_not_mask_original_shipping_error(self, store) -> None:
        draft = _draft("dlq-write-failure")
        store.upsert_draft(draft)
        provider_error = InPostBusinessError(
            "original provider rejection",
            order_id=draft["id"],
            courier="inpost",
            action="create_shipment",
        )

        with (
            patch.object(webhooks, "_get_sender", return_value=_SENDER),
            patch.object(webhooks, "_run_inpost", side_effect=provider_error),
            patch.object(store, "enqueue_dlq", side_effect=RuntimeError("DLQ unavailable")),
            pytest.raises(InPostBusinessError) as caught,
        ):
            _execute_application(draft["id"], store, object())

        assert caught.value is provider_error
        assert store.get_draft(draft["id"])["status"] == "error"


class TestProviderDispatchCharacterization:
    @pytest.mark.parametrize(
        ("courier", "expected"),
        [
            pytest.param("inpost", "inpost", id="inpost"),
            pytest.param("allegro_delivery", "allegro", id="allegro-delivery"),
            pytest.param("apaczka", "apaczka", id="apaczka"),
            pytest.param("unknown-provider", "apaczka", id="unknown-falls-back-to-apaczka"),
            pytest.param("", "apaczka", id="empty-falls-back-to-apaczka"),
            pytest.param(None, "apaczka", id="blank-falls-back-to-apaczka"),
            pytest.param("__missing__", "apaczka", id="missing-falls-back-to-apaczka"),
        ],
    )
    def test_current_courier_dispatch_table(
        self, store, courier: str | None, expected: str
    ) -> None:
        draft = _draft(f"dispatch-{expected}-{courier}", courier=courier)
        if courier == "__missing__":
            draft.pop("courier")
        store.upsert_draft(draft)
        provider_patch = {
            "courier_draft_id": f"shipment-{expected}",
            "tracking_number": f"TRACK-{expected}",
            "status": "created",
            "error": None,
        }

        with (
            patch.object(webhooks, "_get_sender", return_value=_SENDER),
            patch.object(webhooks, "_run_inpost", return_value=provider_patch) as inpost,
            patch.object(webhooks, "_run_apaczka", return_value=provider_patch) as apaczka,
            patch.object(webhooks, "_run_allegro_delivery", return_value=provider_patch) as allegro,
        ):
            _execute_application(draft["id"], store, object())

        calls = {
            "inpost": inpost.call_count,
            "apaczka": apaczka.call_count,
            "allegro": allegro.call_count,
        }
        assert calls == {
            "inpost": int(expected == "inpost"),
            "apaczka": int(expected == "apaczka"),
            "allegro": int(expected == "allegro"),
        }


class TestPendingExecutionCharacterization:
    def test_inpost_pending_draft_resumes_without_another_post(self, store) -> None:
        draft = _draft(
            "pending-inpost",
            status="pending_confirmation",
            courier_draft_id="existing-shipx-id",
        )
        store.upsert_draft(draft)

        with (
            patch.object(webhooks, "_get_sender", return_value=_SENDER),
            patch.object(webhooks, "get_secret", return_value="token"),
            patch(
                "zdrovena.common.inpost.InPostClient.get_shipment",
                return_value={"id": "existing-shipx-id", "tracking_number": "TRACK-RESUMED"},
            ),
            patch("zdrovena.common.inpost.InPostClient.create_kurier_shipment") as create_kurier,
            patch("zdrovena.common.inpost.InPostClient.create_paczkomat_shipment") as create_locker,
        ):
            result = _execute_application(draft["id"], store, object())

        create_kurier.assert_not_called()
        create_locker.assert_not_called()
        assert result["status"] == "created"
        assert result["tracking_number"] == "TRACK-RESUMED"

    def test_allegro_pending_command_does_not_create_another_command(self, store) -> None:
        draft = _draft(
            "pending-allegro",
            courier="allegro_delivery",
            status="pending_confirmation",
            allegro_command_id="existing-command-id",
        )
        store.upsert_draft(draft)

        with (
            patch.object(webhooks, "_get_sender", return_value=_SENDER),
            patch.object(webhooks, "_get_allegro_client") as get_client,
        ):
            result = _execute_application(draft["id"], store, object())

        get_client.assert_not_called()
        assert result["status"] == "pending_confirmation"
        assert result["allegro_command_id"] == "existing-command-id"


class _OriginRecordingPatch(dict[str, Any]):
    def __init__(self, events: list[str], *args: Any, **kwargs: Any) -> None:
        self._events = events
        super().__init__(*args, **kwargs)

    def __setitem__(self, key: str, value: Any) -> None:
        if key == "shipment_origin":
            self._events.append("origin")
        super().__setitem__(key, value)


class TestExecutionFinalizationCharacterization:
    @pytest.mark.parametrize(
        ("tracking_number", "expected"),
        [
            pytest.param(
                "TRACK-FINAL",
                [
                    "provider",
                    "origin",
                    "persist",
                    "reload",
                    "shipment.created",
                    "tracking",
                    "allegro-sync",
                ],
                id="with-tracking",
            ),
            pytest.param(
                None,
                [
                    "provider",
                    "origin",
                    "persist",
                    "reload",
                    "shipment.created",
                    "allegro-sync",
                ],
                id="without-tracking",
            ),
        ],
    )
    def test_finalization_order(self, tracking_number: str | None, expected: list[str]) -> None:
        draft = _draft("finalization-order")
        events: list[str] = []
        repository = MagicMock()
        final_patch = _OriginRecordingPatch(
            events,
            {
                "courier_draft_id": "shipment-final",
                "tracking_number": tracking_number,
                "status": "created" if tracking_number else "pending_confirmation",
                "error": None,
            },
        )
        lookup_count = 0

        def get_draft(_draft_id: str) -> dict[str, Any]:
            nonlocal lookup_count
            lookup_count += 1
            if lookup_count == 1:
                return draft
            events.append("reload")
            return {**draft, **final_patch}

        def update_draft(_draft_id: str, fields: dict[str, Any]) -> bool:
            assert fields["shipment_origin"] == "system"
            events.append("persist")
            return True

        def run_provider(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            events.append("provider")
            return final_patch

        repository.get_draft.side_effect = get_draft
        repository.try_claim_execution.return_value = True
        repository.update_draft.side_effect = update_draft

        with (
            patch.object(webhooks, "_get_sender", return_value=_SENDER),
            patch.object(webhooks, "_run_inpost", side_effect=run_provider),
            patch.object(
                webhooks,
                "log_event",
                side_effect=lambda name, **_fields: events.append(name),
            ),
            patch.object(
                webhooks,
                "_emit_tracking_assigned",
                side_effect=lambda *_args: events.append("tracking"),
            ),
            patch.object(
                webhooks,
                "_maybe_push_tracking_to_allegro",
                side_effect=lambda _draft: events.append("allegro-sync"),
            ),
        ):
            _execute_application(draft["id"], repository, object())

        assert events == expected

    def test_allegro_tracking_sync_failure_remains_best_effort(self, store) -> None:
        draft = _draft(
            "best-effort-allegro-sync",
            source="allegro",
            external_order_id="allegro-order-42",
        )
        store.upsert_draft(draft)
        allegro = MagicMock()
        allegro.create_shipment.side_effect = RuntimeError("Allegro tracking API unavailable")

        with (
            patch.object(webhooks, "_get_sender", return_value=_SENDER),
            patch.object(
                webhooks,
                "_run_inpost",
                return_value={
                    "courier_draft_id": "shipment-with-local-success",
                    "tracking_number": "TRACK-LOCAL-SUCCESS",
                    "status": "created",
                    "error": None,
                },
            ),
            patch.object(webhooks, "_get_allegro_client", return_value=allegro),
        ):
            result = _execute_application(draft["id"], store, object())

        assert result["status"] == "created"
        assert store.get_draft(draft["id"])["tracking_number"] == "TRACK-LOCAL-SUCCESS"
        allegro.create_shipment.assert_called_once()
