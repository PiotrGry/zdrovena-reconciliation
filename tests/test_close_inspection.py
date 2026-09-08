"""Tests for the read-only period inspection endpoint.

The point of this endpoint is that asking how a period stands is a *question*,
not a move in the state machine. Every test here that matters is really the
same assertion from a different angle: reading changed nothing.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("AZURE_AUTH_DISABLED", "true")

from zdrovena.api.main import app
from zdrovena.common.storage import LocalStorageService
from zdrovena.month_closing.inspection import build_document, build_issue
from zdrovena.month_closing.run_store import CloseRunStore, new_close_run

# Built with the production helpers rather than by hand: the shape the endpoint
# has to serialise is then the real one, not a guess that drifts from it.
_INSPECTION = {
    "documents": [build_document("sales", "sales", "Sprzedaż", "ok")],
    "issues": [],
    "metrics": {"ready": True},
}


@pytest.fixture()
def api(tmp_path):
    storage = LocalStorageService(root=tmp_path / "storage")
    with patch("zdrovena.api.deps._storage_singleton", return_value=storage):
        with TestClient(app, raise_server_exceptions=True) as client:
            yield client, tmp_path


@pytest.fixture()
def store(tmp_path) -> CloseRunStore:
    return CloseRunStore(local_root=tmp_path / "runs")


def _get(client: TestClient, *, year: int = 2026, month: int = 8, result=None):
    with patch("zdrovena.api.routers.close.MonthCloseInspector") as inspector:
        inspector.return_value.inspect.return_value = result or dict(_INSPECTION)
        return client.get(f"/api/close/inspection?year={year}&month={month}")


class TestReadingChangesNothing:
    def test_a_period_nobody_started_can_still_be_inspected(self, api, store):
        # The main use: "how do I stand with August?" before any close began.
        client, _ = api
        with patch("zdrovena.api.routers.close.CloseRunStore", return_value=store):
            resp = _get(client)

        assert resp.status_code == 200
        assert resp.json()["run"] is None
        assert resp.json()["metrics"] == {"ready": True}

    def test_inspecting_does_not_create_a_run(self, api, store):
        # GET /close/workflow creates one through get_or_create. This must not.
        client, _ = api
        assert store.get(2026, 8) is None

        with patch("zdrovena.api.routers.close.CloseRunStore", return_value=store):
            _get(client)

        assert store.get(2026, 8) is None

    def test_an_existing_run_is_reported_but_not_touched(self, api, store):
        client, _ = api
        run = new_close_run(2026, 8, "someone@example.test")
        store.save(run)
        before = store.get(2026, 8)

        with patch("zdrovena.api.routers.close.CloseRunStore", return_value=store):
            resp = _get(client)

        after = store.get(2026, 8)
        assert resp.json()["run"]["status"] == before["status"]
        assert after["rev"] == before["rev"]
        assert after["updated_at"] == before["updated_at"]

    def test_a_stale_active_step_is_not_marked_failed(self, api, store):
        # get_or_create flips a step abandoned for 30 minutes to "failed" and
        # saves. Inspection reports what is there and leaves the verdict alone.
        client, _ = api
        run = new_close_run(2026, 8, "someone@example.test")
        run["active_action"] = "sales"
        run["steps"]["sales"]["status"] = "running"
        run["steps"]["sales"]["started_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=2)
        ).isoformat()
        store.save(run)

        with patch("zdrovena.api.routers.close.CloseRunStore", return_value=store):
            resp = _get(client)

        assert store.get(2026, 8)["steps"]["sales"]["status"] == "running"
        assert resp.json()["run"]["steps"]["sales"]["status"] == "running"


class TestWhatItReports:
    def test_the_result_says_when_it_was_computed(self, api, store):
        client, _ = api
        with patch("zdrovena.api.routers.close.CloseRunStore", return_value=store):
            resp = _get(client)

        computed_at = resp.json()["computed_at"]
        assert datetime.fromisoformat(computed_at).tzinfo is not None

    def test_two_calls_report_two_different_moments(self, api, store):
        # Proves the answer is computed now rather than replayed from a run.
        client, _ = api
        with patch("zdrovena.api.routers.close.CloseRunStore", return_value=store):
            first = _get(client).json()["computed_at"]
            second = _get(client).json()["computed_at"]

        assert first != second

    def test_an_unreachable_fakturownia_blocks_rather_than_looks_empty(self, api, store):
        # A read that fails open would show a clean month and send the operator
        # to build a package out of nothing.
        client, _ = api
        unavailable = {
            "documents": [],
            "issues": [
                build_issue(
                    "fakturownia-unavailable",
                    "blocker",
                    "Nie udało się sprawdzić Fakturowni: timeout",
                )
            ],
            "metrics": {"ready": False},
        }
        with patch("zdrovena.api.routers.close.CloseRunStore", return_value=store):
            resp = _get(client, result=unavailable)

        assert resp.status_code == 200
        assert resp.json()["metrics"]["ready"] is False
        assert resp.json()["issues"][0]["severity"] == "blocker"

    def test_the_period_is_echoed_back(self, api, store):
        client, _ = api
        with patch("zdrovena.api.routers.close.CloseRunStore", return_value=store):
            resp = _get(client, year=2025, month=12)

        assert (resp.json()["year"], resp.json()["month"]) == (2025, 12)


class TestInputValidation:
    @pytest.mark.parametrize("year,month", [(2026, 0), (2026, 13), (1999, 3)])
    def test_a_period_outside_the_supported_range_is_refused(self, api, year, month):
        client, _ = api
        resp = client.get(f"/api/close/inspection?year={year}&month={month}")
        assert resp.status_code == 422


def test_the_inspector_is_built_for_the_requested_period(api, store):
    client, _ = api
    with patch("zdrovena.api.routers.close.CloseRunStore", return_value=store):
        with patch("zdrovena.api.routers.close.MonthCloseInspector") as inspector:
            inspector.return_value.inspect.return_value = dict(_INSPECTION)
            client.get("/api/close/inspection?year=2025&month=4")

    assert inspector.call_args.args[:2] == (2025, 4)


def test_the_endpoint_is_open_to_the_viewer_role():
    """Checking where a period stands is not an accountant's action."""
    source = Path("zdrovena/api/routers/close.py").read_text(encoding="utf-8")
    marker = source.index('"/inspection"')
    signature = source[marker : marker + 700]
    assert "require_viewer_or_above" in signature
