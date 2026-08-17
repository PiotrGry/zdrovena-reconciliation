"""Tests for operator-driven month-close actions."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from zdrovena.common.storage import LocalStorageService
from zdrovena.month_closing.orchestrator import CloseReport
from zdrovena.month_closing.run_store import CloseRunStore, RunBusyError
from zdrovena.month_closing.workflow import (
    MonthCloseInspector,
    MonthCloseWorkflow,
    WaiverTargetError,
)


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
    assert "Najpierw zakończ lub pomiń etapy" in run["steps"]["package"]["message"]


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


def _blocked_run(workflow):
    """Seed a run where preflight found a blocker and one stage failed."""
    run = workflow.get_run(2026, 6, "owner@example.com")
    run["status"] = "needs_input"
    run["steps"]["check"]["status"] = "done"
    run["steps"]["sales"]["status"] = "done"
    run["steps"]["reports"]["status"] = "done"
    run["steps"]["bank"]["status"] = "done"
    run["steps"]["costs"]["status"] = "failed"
    run["issues"] = [
        {
            "id": "manual-missing-Shopify",
            "severity": "blocker",
            "message": "Brakuje ręcznego dokumentu: Shopify.",
            "stage": "check",
        }
    ]
    workflow.store.save(run)
    return run


def test_waived_stage_and_issue_unblock_the_package(tmp_path):
    workflow = _workflow(tmp_path)
    seeded = _blocked_run(workflow)
    orchestrator = MagicMock()
    orchestrator.execute_stage.return_value = CloseReport(
        zip_path=Path("paczka.zip"),
        zip_files=["a"],
    )
    # The package stage always re-inspects, so the blocker keeps coming back
    # until the operator waives it explicitly.
    inspected = {"documents": [], "issues": seeded["issues"], "metrics": {"ready": False}}

    with (
        patch(
            "zdrovena.month_closing.workflow.MonthCloseOrchestrator",
            return_value=orchestrator,
        ),
        patch(
            "zdrovena.month_closing.workflow.MonthCloseInspector.inspect",
            return_value=inspected,
        ),
    ):
        run = workflow.perform(2026, 6, "package", "owner@example.com")
        assert "Najpierw zakończ lub pomiń etapy: costs." in run["steps"]["package"]["message"]

        workflow.waive(2026, 6, "step:costs", "owner@example.com")
        run = workflow.perform(2026, 6, "package", "owner@example.com")
        assert "zignoruj blokujące problemy" in run["steps"]["package"]["message"]

        run = workflow.waive(2026, 6, "issue:manual-missing-Shopify", "owner@example.com")
        shopify = next(i for i in run["issues"] if i["id"] == "manual-missing-Shopify")
        assert shopify["waived"] is True
        assert shopify["waived_by"] == "owner@example.com"
        assert run["steps"]["costs"]["waived"] is True

        run = workflow.perform(2026, 6, "package", "owner@example.com")

    assert run["steps"]["package"]["status"] == "done"
    assert orchestrator.execute_stage.call_args.args == ("package",)


def test_unwaive_restores_the_gate(tmp_path):
    workflow = _workflow(tmp_path)
    _blocked_run(workflow)
    workflow.waive(2026, 6, "step:costs", "owner@example.com")

    run = workflow.unwaive(2026, 6, "step:costs", "owner@example.com")

    assert run["waivers"] == []
    assert run["steps"]["costs"]["waived"] is False
    run = workflow.perform(2026, 6, "package", "owner@example.com")
    assert "Najpierw zakończ lub pomiń etapy: costs." in run["steps"]["package"]["message"]


def test_unwaiving_the_last_blocker_restores_needs_input(tmp_path):
    workflow = _workflow(tmp_path)
    _blocked_run(workflow)

    waived = workflow.waive(2026, 6, "issue:manual-missing-Shopify", "owner@example.com")
    assert waived["metrics"]["ready"] is True
    assert waived["status"] == "ready"

    restored = workflow.unwaive(2026, 6, "issue:manual-missing-Shopify", "owner@example.com")

    assert restored["metrics"]["ready"] is False
    assert restored["status"] == "needs_input"


def test_rerunning_a_stage_drops_the_waivers_it_owns(tmp_path):
    workflow = _workflow(tmp_path)
    _blocked_run(workflow)
    workflow.waive(2026, 6, "step:costs", "owner@example.com")
    workflow.waive(2026, 6, "issue:manual-missing-Shopify", "owner@example.com")

    orchestrator = MagicMock()
    orchestrator.execute_stage.return_value = CloseReport(cost_missing_vendors=["Shopify"])
    with patch(
        "zdrovena.month_closing.workflow.MonthCloseOrchestrator",
        return_value=orchestrator,
    ):
        run = workflow.perform(2026, 6, "costs", "owner@example.com")

    # The stage waiver covered a result the operator has now replaced, so it is
    # gone; the preflight issue was not re-evaluated, so its waiver survives.
    assert run["steps"]["costs"]["waived"] is False
    assert [waiver["target"] for waiver in run["waivers"]] == ["issue:manual-missing-Shopify"]


def test_rerunning_preflight_drops_waivers_on_its_own_issues(tmp_path):
    workflow = _workflow(tmp_path)
    _blocked_run(workflow)
    workflow.waive(2026, 6, "issue:manual-missing-Shopify", "owner@example.com")

    with patch(
        "zdrovena.month_closing.workflow.MonthCloseInspector.inspect",
        return_value={"documents": [], "issues": [], "metrics": {"ready": True}},
    ):
        run = workflow.perform(2026, 6, "check", "owner@example.com")

    assert run["waivers"] == []


def test_send_with_waived_warnings_needs_no_override_reason(tmp_path):
    workflow = _workflow(tmp_path)
    run = workflow.get_run(2026, 6, "owner@example.com")
    run["steps"]["package"]["status"] = "done"
    run["issues"] = [
        {
            "id": "generated",
            "severity": "warning",
            "message": "Dostępny jest tylko render.",
            "stage": "check",
        }
    ]
    workflow.store.save(run)
    workflow.waive(2026, 6, "issue:generated", "owner@example.com")

    orchestrator = MagicMock()
    orchestrator.execute_stage.return_value = CloseReport(email_sent=True)
    inspected = {"documents": [], "issues": run["issues"], "metrics": {"ready": True}}
    with (
        patch(
            "zdrovena.month_closing.workflow.MonthCloseOrchestrator",
            return_value=orchestrator,
        ),
        patch(
            "zdrovena.month_closing.workflow.MonthCloseInspector.inspect",
            return_value=inspected,
        ),
    ):
        updated = workflow.perform(2026, 6, "send", "owner@example.com", confirm=True)

    assert updated["steps"]["send"]["status"] == "done"


def test_send_records_waivers_in_close_history(tmp_path):
    workflow = _workflow(tmp_path)
    run = workflow.get_run(2026, 6, "owner@example.com")
    run["steps"]["package"]["status"] = "done"
    run["issues"] = [
        {
            "id": "generated",
            "severity": "warning",
            "message": "Dostępny jest tylko render.",
            "stage": "check",
        }
    ]
    workflow.store.save(run)
    workflow.waive(2026, 6, "issue:generated", "owner@example.com")

    orchestrator = MagicMock()
    orchestrator.execute_stage.return_value = CloseReport(email_sent=True)
    history = MagicMock()
    with (
        patch(
            "zdrovena.month_closing.workflow.MonthCloseOrchestrator",
            return_value=orchestrator,
        ),
        patch(
            "zdrovena.month_closing.workflow.MonthCloseInspector.inspect",
            return_value={"documents": [], "issues": run["issues"], "metrics": {"ready": True}},
        ),
        patch("zdrovena.month_closing.workflow.append_close_history", history),
    ):
        workflow.perform(2026, 6, "send", "owner@example.com", confirm=True)

    entry = history.call_args.args[1]
    assert entry["waiver_count"] == 1
    assert entry["waivers"][0]["target"] == "issue:generated"
    assert entry["waivers"][0]["user"] == "owner@example.com"


def test_package_and_send_stages_cannot_be_waived(tmp_path):
    workflow = _workflow(tmp_path)
    workflow.get_run(2026, 6, "owner@example.com")

    for target in ("step:package", "step:send", "step:nonsense", "issue:nieznany", "bzdura"):
        with pytest.raises(WaiverTargetError):
            workflow.waive(2026, 6, target, "owner@example.com")


def test_waiver_is_rejected_while_a_stage_is_running(tmp_path):
    workflow = _workflow(tmp_path)
    run = workflow.get_run(2026, 6, "owner@example.com")
    run["active_action"] = "costs"
    workflow.store.save(run)

    with pytest.raises(RunBusyError):
        workflow.waive(2026, 6, "step:costs", "owner@example.com")


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
