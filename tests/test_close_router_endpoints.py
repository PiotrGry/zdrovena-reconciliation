"""HTTP edge of the /close router: error mapping, state, history and reset.

Complements tests/test_fastapi.py (happy-path close and workflow actions) and
tests/test_close_inspection.py with the branches that translate domain
refusals into status codes, plus the read/delete endpoints for checkpoint
state and close history.
"""

from __future__ import annotations

import json
import os
from typing import ClassVar
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("AZURE_AUTH_DISABLED", "true")

from tests.test_fastapi import _make_report
from zdrovena.api.main import app
from zdrovena.common.storage import LocalStorageService
from zdrovena.month_closing import close_history
from zdrovena.month_closing.config import POLISH_MONTHS
from zdrovena.month_closing.run_store import RunBusyError, RunConflictError, new_close_run
from zdrovena.month_closing.workflow import EmailAttemptResolutionError

ROUTER = "zdrovena.api.routers.close"
DEV = "dev@localhost"


@pytest.fixture()
def storage(tmp_path):
    return LocalStorageService(root=tmp_path / "storage")


@pytest.fixture()
def history_file(tmp_path, monkeypatch):
    """Keep close history away from the developer's ~/.zdrovena file."""
    monkeypatch.delenv("AZURE_STORAGE_CONNECTION_STRING", raising=False)
    monkeypatch.delenv("AZURE_STORAGE_ACCOUNT_URL", raising=False)
    path = tmp_path / "history" / "close-history.json"
    monkeypatch.setattr(close_history, "_LOCAL_FILE", path)
    return path


@pytest.fixture()
def client(storage, history_file, tmp_path):
    with (
        patch("zdrovena.api.deps._storage_singleton", return_value=storage),
        patch(f"{ROUTER}.get_storage_service", return_value=storage),
        patch(f"{ROUTER}.BASE_DIR", tmp_path / "faktury"),
        TestClient(app, raise_server_exceptions=True) as test_client,
    ):
        yield test_client


def _history(history_file) -> list[dict]:
    if not history_file.exists():
        return []
    return json.loads(history_file.read_text(encoding="utf-8"))


def _seed_history(history_file, entries: list[dict]) -> None:
    history_file.parent.mkdir(parents=True, exist_ok=True)
    history_file.write_text(json.dumps(entries), encoding="utf-8")


# ── POST /close: pipeline failures ────────────────────────────────────────────


class TestRunCloseFailures:
    BODY: ClassVar[dict] = {"year": 2026, "month": 3, "dry_run": True}

    def test_runtime_error_is_422_and_recorded_as_error(self, client, history_file):
        with patch(f"{ROUTER}.MonthCloseOrchestrator") as orchestrator:
            orchestrator.return_value.report = _make_report(steps_completed=["step_1"])
            orchestrator.return_value.execute.side_effect = RuntimeError("Brak wyciągu")
            response = client.post("/api/close", json=self.BODY)

        assert response.status_code == 422
        detail = response.json()["detail"]
        assert detail["blockers"] == ["Brak wyciągu"]
        assert detail["log_lines"] == []

        entries = _history(history_file)
        assert len(entries) == 1
        assert entries[0]["status"] == "error"
        assert entries[0]["year"] == 2026
        assert entries[0]["month"] == 3

    def test_unexpected_exception_is_500_and_not_recorded(self, client, history_file):
        with patch(f"{ROUTER}.MonthCloseOrchestrator") as orchestrator:
            orchestrator.return_value.execute.side_effect = KeyError("boom")
            response = client.post("/api/close", json=self.BODY)

        assert response.status_code == 500
        detail = response.json()["detail"]
        assert detail["blockers"] == ["Nieoczekiwany błąd: 'boom'"]
        assert detail["log_lines"] == []
        assert _history(history_file) == []


# ── /close/workflow/email-attempt ─────────────────────────────────────────────


class TestResolveEmailAttempt:
    URL = "/api/close/workflow/email-attempt"

    def test_delivered_resolution_returns_run(self, client):
        run = new_close_run(2026, 3, DEV)
        with patch(f"{ROUTER}.MonthCloseWorkflow") as workflow:
            workflow.return_value.resolve_email_attempt.return_value = run
            response = client.post(
                self.URL,
                json={"year": 2026, "month": 3, "delivered": True, "note": "widziałem w skrzynce"},
            )

        assert response.status_code == 200
        assert response.json()["run_id"] == run["run_id"]
        workflow.return_value.resolve_email_attempt.assert_called_once_with(
            2026, 3, delivered=True, by=DEV, note="widziałem w skrzynce"
        )

    def test_no_unresolved_attempt_is_409(self, client):
        with patch(f"{ROUTER}.MonthCloseWorkflow") as workflow:
            workflow.return_value.resolve_email_attempt.side_effect = EmailAttemptResolutionError(
                "Brak nierozstrzygniętej próby wysyłki."
            )
            response = client.post(self.URL, json={"year": 2026, "month": 3, "delivered": False})

        assert response.status_code == 409
        assert response.json()["detail"] == "Brak nierozstrzygniętej próby wysyłki."

    @pytest.mark.parametrize(
        "error", [RunBusyError("Etap send trwa."), RunConflictError("Run zmienił się.")]
    )
    def test_busy_or_conflicting_run_is_409(self, client, error):
        with patch(f"{ROUTER}.MonthCloseWorkflow") as workflow:
            workflow.return_value.resolve_email_attempt.side_effect = error
            response = client.post(self.URL, json={"year": 2026, "month": 3, "delivered": True})

        assert response.status_code == 409
        assert response.json()["detail"] == str(error)

    def test_invalid_month_is_rejected_before_workflow(self, client):
        with patch(f"{ROUTER}.MonthCloseWorkflow") as workflow:
            response = client.post(self.URL, json={"year": 2026, "month": 13, "delivered": True})

        assert response.status_code == 422
        workflow.assert_not_called()


# ── DELETE /close/workflow/waivers ────────────────────────────────────────────


class TestRemoveWaiver:
    @pytest.mark.parametrize(
        "error", [RunBusyError("Etap costs trwa."), RunConflictError("Run zmienił się.")]
    )
    def test_busy_or_conflicting_run_is_409(self, client, error):
        with patch(f"{ROUTER}.MonthCloseWorkflow") as workflow:
            workflow.return_value.unwaive.side_effect = error
            response = client.request(
                "DELETE",
                "/api/close/workflow/waivers",
                params={"year": 2026, "month": 3, "target": "step:costs"},
            )

        assert response.status_code == 409
        assert response.json()["detail"] == str(error)


# ── POST /close/workflow/reset ────────────────────────────────────────────────


class TestResetWorkflow:
    URL = "/api/close/workflow/reset"

    def test_idle_run_is_reset(self, client):
        current = new_close_run(2026, 3, DEV)
        fresh = new_close_run(2026, 3, DEV)
        with patch(f"{ROUTER}.MonthCloseWorkflow") as workflow:
            workflow.return_value.get_run.return_value = current
            workflow.return_value.reset.return_value = fresh
            response = client.post(self.URL, json={"year": 2026, "month": 3})

        assert response.status_code == 200
        assert response.json()["run_id"] == fresh["run_id"]
        workflow.return_value.reset.assert_called_once_with(2026, 3, DEV)

    def test_reset_during_running_stage_is_409(self, client):
        current = new_close_run(2026, 3, DEV)
        current["active_action"] = "costs"
        with patch(f"{ROUTER}.MonthCloseWorkflow") as workflow:
            workflow.return_value.get_run.return_value = current
            response = client.post(self.URL, json={"year": 2026, "month": 3})

        assert response.status_code == 409
        assert "podczas wykonywania etapu" in response.json()["detail"]
        workflow.return_value.reset.assert_not_called()


# ── GET /close/state ──────────────────────────────────────────────────────────


class TestCloseState:
    def test_fresh_month_has_no_completed_steps(self, client):
        response = client.get("/api/close/state", params={"year": 2026, "month": 4})

        assert response.status_code == 200
        assert response.json() == {"completed_steps": []}

    def test_local_checkpoint_is_reported(self, client, tmp_path):
        month_dir = tmp_path / "faktury" / "2026" / POLISH_MONTHS[4]
        month_dir.mkdir(parents=True)
        (month_dir / ".state.json").write_text(
            json.dumps({"completed_steps": ["sales", "costs"]}), encoding="utf-8"
        )

        response = client.get("/api/close/state", params={"year": 2026, "month": 4})

        assert response.status_code == 200
        assert response.json()["completed_steps"] == ["sales", "costs"]

    def test_invalid_month_is_422(self, client):
        response = client.get("/api/close/state", params={"year": 2026, "month": 0})

        assert response.status_code == 422


# ── /close/history ────────────────────────────────────────────────────────────


class TestCloseHistory:
    ENTRIES: ClassVar[list[dict]] = [
        {"ts": "2026-03-01T10:00:00+00:00", "status": "success"},
        {"ts": "2026-04-01T10:00:00+00:00", "status": "blocked"},
        {"ts": "2026-05-01T10:00:00+00:00", "status": "partial"},
    ]

    def test_empty_history_is_empty_list(self, client):
        response = client.get("/api/close/history")

        assert response.status_code == 200
        assert response.json() == []

    def test_history_is_newest_first_and_limited(self, client, history_file):
        _seed_history(history_file, self.ENTRIES)

        response = client.get("/api/close/history", params={"limit": 2})

        assert response.status_code == 200
        assert [entry["status"] for entry in response.json()] == ["partial", "blocked"]

    def test_limit_out_of_range_is_422(self, client):
        response = client.get("/api/close/history", params={"limit": 0})

        assert response.status_code == 422

    def test_delete_existing_entry_is_204(self, client, history_file):
        _seed_history(history_file, self.ENTRIES)

        response = client.delete(f"/api/close/history/{self.ENTRIES[1]['ts']}")

        assert response.status_code == 204
        assert [entry["status"] for entry in _history(history_file)] == ["success", "partial"]

    def test_delete_missing_entry_is_404(self, client, history_file):
        _seed_history(history_file, self.ENTRIES)

        response = client.delete("/api/close/history/2020-01-01T00:00:00+00:00")

        assert response.status_code == 404
        assert response.json()["detail"] == "Entry not found"
        assert len(_history(history_file)) == 3
