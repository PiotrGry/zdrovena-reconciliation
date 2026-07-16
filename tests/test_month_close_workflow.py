"""Tests for operator-driven month-close actions."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from zdrovena.common.storage import LocalStorageService
from zdrovena.month_closing.orchestrator import CloseReport
from zdrovena.month_closing.run_store import CloseRunStore
from zdrovena.month_closing.workflow import MonthCloseInspector, MonthCloseWorkflow


def _workflow(tmp_path):
    return MonthCloseWorkflow(
        store=CloseRunStore(local_root=tmp_path / "runs"),
        storage=LocalStorageService(root=tmp_path / "files"),
    )


def test_check_persists_documents_and_blocked_status(tmp_path):
    workflow = _workflow(tmp_path)
    inspected = {
        "documents": [
            {
                "id": "bank",
                "category": "bank",
                "label": "PKO",
                "status": "missing",
                "required": True,
                "source": None,
                "file_key": None,
                "message": "Brakuje",
            }
        ],
        "issues": [
            {
                "id": "bank-missing",
                "severity": "blocker",
                "message": "Brakuje wyciągu",
                "stage": "check",
            }
        ],
        "metrics": {"ready": False},
    }

    with patch(
        "zdrovena.month_closing.workflow.MonthCloseInspector.inspect",
        return_value=inspected,
    ):
        run = workflow.perform(2026, 6, "check", "owner@example.com")

    assert run["status"] == "needs_input"
    assert run["steps"]["check"]["status"] == "done"
    assert run["documents"][0]["id"] == "bank"


def test_package_is_blocked_until_collection_steps_are_done(tmp_path):
    workflow = _workflow(tmp_path)
    run = workflow.perform(2026, 6, "package", "owner@example.com")

    assert run["status"] == "needs_input"
    assert run["steps"]["package"]["status"] == "failed"
    assert "Najpierw zakończ etapy" in run["steps"]["package"]["message"]


def test_sales_stage_is_independent_and_persisted(tmp_path):
    workflow = _workflow(tmp_path)
    run = workflow.get_run(2026, 6, "owner@example.com")
    run["steps"]["check"]["status"] = "done"
    workflow.store.save(run)
    report = CloseReport(
        sales_invoice_count=2,
        sales_pdfs_downloaded=2,
        steps_completed=["Sales invoices"],
    )
    orchestrator = MagicMock()
    orchestrator.execute_stage.return_value = report

    with (
        patch(
            "zdrovena.month_closing.workflow.MonthCloseOrchestrator",
            return_value=orchestrator,
        ),
        patch(
            "zdrovena.month_closing.workflow.MonthCloseInspector.inspect",
            return_value={"documents": [], "issues": [], "metrics": {"ready": True}},
        ),
    ):
        updated = workflow.perform(2026, 6, "sales", "owner@example.com")

    orchestrator.execute_stage.assert_called_once_with("sales")
    assert updated["steps"]["sales"]["status"] == "done"
    assert updated["metrics"]["sales_invoice_count"] == 2


def test_send_requires_explicit_confirmation(tmp_path):
    workflow = _workflow(tmp_path)
    run = workflow.get_run(2026, 6, "owner@example.com")
    run["steps"]["package"]["status"] = "done"
    workflow.store.save(run)

    updated = workflow.perform(2026, 6, "send", "owner@example.com", confirm=False)

    assert updated["steps"]["send"]["status"] == "failed"
    assert "potwierdzenia" in updated["steps"]["send"]["message"]


def test_send_with_warnings_requires_override_reason(tmp_path):
    workflow = _workflow(tmp_path)
    run = workflow.get_run(2026, 6, "owner@example.com")
    run["steps"]["package"]["status"] = "done"
    workflow.store.save(run)
    inspected = {
        "documents": [],
        "issues": [
            {
                "id": "generated",
                "severity": "warning",
                "message": "Dostępny jest tylko render.",
                "stage": "check",
            }
        ],
        "metrics": {"ready": True},
    }

    with patch(
        "zdrovena.month_closing.workflow.MonthCloseInspector.inspect",
        return_value=inspected,
    ):
        updated = workflow.perform(
            2026,
            6,
            "send",
            "owner@example.com",
            confirm=True,
        )

    assert updated["steps"]["send"]["status"] == "failed"
    assert "podania powodu" in updated["steps"]["send"]["message"]


def test_preflight_rejects_bank_statement_from_another_period(tmp_path):
    storage = LocalStorageService(root=tmp_path / "files")
    wrong_bank = tmp_path / "Wyciag_na_zadanie_20260801001.pdf"
    wrong_bank.write_bytes(b"%PDF")
    storage.upload(wrong_bank, "faktury/inbox/2026-06/Wyciag_na_zadanie_20260801001.pdf")
    client = MagicMock()
    client.fetch_sales_invoices.return_value = []
    client.fetch_cost_invoices.return_value = []

    with patch(
        "zdrovena.month_closing.workflow.FakturowniaClient.from_keyring",
        return_value=client,
    ):
        inspected = MonthCloseInspector(2026, 6, storage=storage).inspect()

    bank = next(document for document in inspected["documents"] if document["id"] == "bank-pko")
    assert bank["status"] == "invalid"
    assert any("nie pasuje" in issue["message"] for issue in inspected["issues"])
