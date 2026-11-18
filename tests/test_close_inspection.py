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


# ─── Cost documents ──────────────────────────────────────────────────────────
#
# For every cost invoice the package holds the original PDF, a Fakturownia
# render, or nothing. A lost KSeF original once went unnoticed here (#387), so
# each branch of the decision is pinned on its own.


def _vendor(*, original_required: bool):
    from zdrovena.month_closing.config import EXPECTED_VENDORS

    return next(
        vendor
        for vendor in EXPECTED_VENDORS
        if not vendor.skip
        and not vendor.download_glob
        and (vendor.source_policy == "original_required") == original_required
    )


def _vendor_named(name: str):
    from zdrovena.month_closing.config import EXPECTED_VENDORS

    return next(vendor for vendor in EXPECTED_VENDORS if vendor.name == name)


_STRICT = _vendor(original_required=True)
_LENIENT = _vendor(original_required=False)
_COSTS_DIR = "faktury/2026/sierpien/koszty"


class _StoredFile:
    def __init__(self, key: str) -> None:
        self.key = key


def _cost(vendor, *, invoice_id: int = 501, has_attachments: bool = False) -> dict:
    # Fakturownia keeps the vendor of a cost invoice in the buyer fields.
    return {
        "id": invoice_id,
        "number": f"FV/{invoice_id}/08/2026",
        "buyer_name": f"{vendor.pattern.upper()} Sp. z o.o.",
        "buyer_tax_no": "",
        "has_attachments": has_attachments,
    }


def _stem(inv: dict) -> str:
    from zdrovena.common import FakturowniaClient

    return FakturowniaClient.cost_document_stem(inv)


def _cost_documents(costs: list[dict], names: list[str]) -> tuple[list[dict], list[dict]]:
    from zdrovena.month_closing.inspection import MonthCloseInspector

    # The method only reads ``.key`` of the listed files; storage is never touched.
    inspector = MonthCloseInspector(*_PERIOD, storage=object())
    month_files = [_StoredFile(f"{_COSTS_DIR}/{name}") for name in names]
    issues: list[dict] = []
    documents = inspector._cost_documents(costs, month_files, issues)
    return documents, issues


def _cost_document(inv: dict, names: list[str]) -> tuple[dict, list[dict]]:
    documents, issues = _cost_documents([inv], names)
    return next(doc for doc in documents if doc["id"] == f"cost-{inv['id']}"), issues


def _vendor_document_id(vendor) -> str:
    return f"vendor-{vendor.name.casefold().replace(' ', '-')}"


class TestCostDocuments:
    def test_the_vendors_under_test_have_the_intended_policies(self):
        assert _STRICT.source_policy == "original_required"
        assert _LENIENT.source_policy != "original_required"

    @pytest.mark.parametrize("vendor", [_STRICT, _LENIENT], ids=["required", "preferred"])
    @pytest.mark.parametrize("with_render", [False, True], ids=["alone", "with-render"])
    def test_a_stored_original_is_present_without_an_issue(self, vendor, with_render):
        inv = _cost(vendor)
        original = f"{_stem(inv)}__original__ksef.pdf"
        names = [f"{_stem(inv)}.pdf", original] if with_render else [original]

        doc, issues = _cost_document(inv, names)

        assert doc["status"] == "present"
        assert doc["source"] == "Oryginalny załącznik Fakturowni"
        assert doc["file_key"] == f"{_COSTS_DIR}/{original}"
        assert issues == []

    def test_a_render_where_the_original_is_required_is_a_blocker(self):
        # Even with an attachment in Fakturownia: the stored render must go.
        inv = _cost(_STRICT, has_attachments=True)
        render = f"{_stem(inv)}.pdf"

        doc, issues = _cost_document(inv, [render])

        assert doc["status"] == "missing"
        assert doc["source"] == "Wygenerowany PDF — do zastąpienia"
        assert doc["file_key"] == f"{_COSTS_DIR}/{render}"
        assert [(i["id"], i["severity"]) for i in issues] == [
            (f"generated-forbidden-{inv['id']}", "blocker")
        ]

    def test_a_render_where_the_original_is_optional_is_a_warning(self):
        inv = _cost(_LENIENT)
        render = f"{_stem(inv)}.pdf"

        doc, issues = _cost_document(inv, [render])

        assert doc["status"] == "present"
        assert doc["source"] == "Wygenerowany PDF Fakturowni"
        assert doc["file_key"] == f"{_COSTS_DIR}/{render}"
        assert [(i["id"], i["severity"]) for i in issues] == [
            (f"generated-stored-{inv['id']}", "warning")
        ]

    @pytest.mark.parametrize("vendor", [_STRICT, _LENIENT], ids=["required", "preferred"])
    def test_nothing_stored_but_an_attachment_is_fetched_automatically(self, vendor):
        inv = _cost(vendor, has_attachments=True)

        doc, issues = _cost_document(inv, [])

        assert doc["status"] == "available_automatically"
        assert doc["source"] == "Oryginalny załącznik Fakturowni"
        assert doc["file_key"] is None
        assert issues == []

    def test_nothing_at_all_where_the_original_is_required_is_a_blocker(self):
        inv = _cost(_STRICT)

        doc, issues = _cost_document(inv, [])

        assert doc["status"] == "missing"
        assert doc["source"] == "Zoho / wgranie ręczne"
        assert doc["file_key"] is None
        assert [(i["id"], i["severity"]) for i in issues] == [
            (f"original-required-{inv['id']}", "blocker")
        ]

    def test_nothing_at_all_where_the_original_is_optional_falls_back_to_a_render(self):
        inv = _cost(_LENIENT)

        doc, issues = _cost_document(inv, [])

        assert doc["status"] == "available_automatically"
        assert doc["source"] == "Wygenerowany PDF Fakturowni"
        assert doc["file_key"] is None
        assert [(i["id"], i["severity"]) for i in issues] == [
            (f"generated-fallback-{inv['id']}", "warning")
        ]

    def test_an_original_is_recognised_whatever_the_letter_case(self):
        inv = _cost(_STRICT)
        original = f"{_stem(inv).upper()}__ORIGINAL__X.PDF"

        doc, issues = _cost_document(inv, [original])

        assert doc["status"] == "present"
        assert doc["source"] == "Oryginalny załącznik Fakturowni"
        assert doc["file_key"] == f"{_COSTS_DIR}/{original}"
        assert issues == []

    def test_a_render_is_recognised_whatever_the_letter_case(self):
        inv = _cost(_STRICT)
        render = f"{_stem(inv).upper()}.PDF"

        doc, issues = _cost_document(inv, [render])

        assert doc["status"] == "missing"
        assert doc["source"] == "Wygenerowany PDF — do zastąpienia"
        assert doc["file_key"] == f"{_COSTS_DIR}/{render}"
        assert [(i["id"], i["severity"]) for i in issues] == [
            (f"generated-forbidden-{inv['id']}", "blocker")
        ]

    def test_a_vendor_with_an_invoice_gets_no_separate_vendor_document(self):
        inv = _cost(_LENIENT, has_attachments=True)

        documents, _ = _cost_documents([inv], [])

        assert _vendor_document_id(_LENIENT) not in [doc["id"] for doc in documents]

    def test_an_expected_vendor_without_an_invoice_is_left_to_the_collector(self):
        vendor = _vendor_named("Apaczka")

        documents, issues = _cost_documents([], [])

        doc = next(doc for doc in documents if doc["id"] == _vendor_document_id(vendor))
        assert doc["status"] == "available_automatically"
        assert doc["source"] == "Zoho Mail"
        assert doc["file_key"] is None
        assert issues == []

    @pytest.mark.parametrize("by", ["pattern", "name"])
    def test_an_expected_vendor_without_an_invoice_is_present_when_stored(self, by):
        # Apaczka invoices come from Alsendo: the name and the pattern differ,
        # and either one in a stored file name is enough.
        vendor = _vendor_named("Apaczka")
        assert vendor.pattern.casefold() not in vendor.name.casefold()
        needle = vendor.pattern if by == "pattern" else vendor.name

        documents, issues = _cost_documents([], [f"Faktura_{needle.upper()}_08.pdf"])

        doc = next(doc for doc in documents if doc["id"] == _vendor_document_id(vendor))
        assert doc["status"] == "present"
        assert doc["source"] == "Pobrany dokument"
        assert doc["file_key"] is None
        assert issues == []
