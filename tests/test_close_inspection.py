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


# ── Bank statement detection (pins hotfix #480, a584cec) ──────────────────────
#
# The statement used to be recognised by file name alone, so a PKO export saved
# under any other name left the period "missing" a statement it already had.
# These tests drive the real inspector over a real month-scoped inbox; only the
# PDF text extraction is stubbed, so the content check itself still runs.

_PERIOD = (2026, 8)
_INBOX = "faktury/inbox/2026-08"
_PKO_TEXT = "PKO Bank Polski SA\nWyciąg z rachunku nr 8/2026\nSaldo początkowe"
_INVOICE_TEXT = "Faktura VAT nr FV/12/08/2026\nSprzedawca: Dostawca Sp. z o.o."


def _fake_extract_text(path: Path) -> str:
    # The "PDFs" in these tests are plain text standing in for the extracted text.
    return path.read_text(encoding="utf-8")


def _inspect_bank(tmp_path: Path, files: dict[str, str]) -> tuple[dict, list[dict]]:
    """Inspect a period whose inbox holds ``files``; return the bank document and issues."""
    from zdrovena.month_closing.inspection import MonthCloseInspector

    storage = LocalStorageService(root=tmp_path / "storage")
    for name, text in files.items():
        target = storage.root / _INBOX / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    with (
        patch(
            "zdrovena.month_closing.preflight.extract_text",
            side_effect=_fake_extract_text,
            create=True,
        ),
        patch(
            "zdrovena.month_closing.inspection.FakturowniaClient.from_keyring",
            side_effect=RuntimeError("offline in tests"),
        ),
    ):
        result = MonthCloseInspector(*_PERIOD, storage=storage).inspect()

    bank = next(doc for doc in result["documents"] if doc["id"] == "bank-pko")
    bank_issues = [issue for issue in result["issues"] if issue["id"].startswith("bank-")]
    return bank, bank_issues


class TestBankStatementDetection:
    def test_a_statement_is_found_by_its_content_not_its_name(self, tmp_path):
        # Neither "wyciag" nor "pko" in the name: only the content says what it is.
        bank, issues = _inspect_bank(tmp_path, {"historia_sierpien.pdf": _PKO_TEXT})

        assert bank["status"] == "present"
        assert bank["message"] == "historia_sierpien.pdf"
        assert bank["file_key"] == f"{_INBOX}/historia_sierpien.pdf"
        assert issues == []

    def test_an_invoice_in_the_inbox_is_not_taken_for_a_statement(self, tmp_path):
        bank, issues = _inspect_bank(tmp_path, {"faktura_08.pdf": _INVOICE_TEXT})

        assert bank["status"] == "missing"
        assert [issue["id"] for issue in issues] == ["bank-missing"]

    def test_two_candidate_statements_are_ambiguous_and_both_are_named(self, tmp_path):
        bank, issues = _inspect_bank(
            tmp_path,
            {
                "Wyciag_8_12345678_202608311.pdf": _PKO_TEXT,
                "historia_sierpien.pdf": _PKO_TEXT,
            },
        )

        assert bank["status"] == "invalid"
        assert len(issues) == 1
        issue = issues[0]
        assert issue["id"] == "bank-ambiguous"
        assert issue["severity"] == "blocker"
        assert "Wyciag_8_12345678_202608311.pdf" in issue["message"]
        assert "historia_sierpien.pdf" in issue["message"]

    def test_an_on_demand_statement_for_another_month_is_invalid(self, tmp_path):
        # Legacy on-demand names carry the generation date, which for August is
        # in September; a June date belongs to May, not to the selected period.
        name = "Wyciag_na_zadanie_12345678_20260601123.pdf"
        bank, issues = _inspect_bank(tmp_path, {name: _PKO_TEXT})

        assert bank["status"] == "invalid"
        assert bank["message"] == f"{name} nie pasuje do wybranego okresu."
        assert [issue["id"] for issue in issues] == ["bank-invalid"]
        assert issues[0]["severity"] == "blocker"
        assert issues[0]["message"] == f"Wyciąg {name} nie pasuje do okresu 2026-08."

    def test_no_candidate_at_all_is_missing(self, tmp_path):
        bank, issues = _inspect_bank(tmp_path, {})

        assert bank["status"] == "missing"
        assert bank["file_key"] is None
        assert [issue["id"] for issue in issues] == ["bank-missing"]
        assert issues[0]["message"] == "Brakuje wyciągu PKO BP za 2026-08."
