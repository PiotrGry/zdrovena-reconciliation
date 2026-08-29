"""Month-close asks the warehouse question it never used to ask (issue #308).

A month could be closed and mailed to the accountant with an invoice that has no
WZ behind it, or a WZ nothing was billed for. The audit that finds those exists
in `zdrovena audit`, a separate CLI somebody has to remember to run.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from zdrovena.month_closing.warehouse_audit import (
    DEFAULT_SEVERITY,
    resolve_severity,
    warehouse_issues,
)


def _invoice(inv_id: int, number: str) -> dict:
    return {
        "id": inv_id,
        "number": number,
        # A name the bottle parser actually recognises — otherwise the
        # invoice has no bottle positions and is correctly ignored.
        "positions": [{"name": "Woda Humio 500ml x 12", "quantity": 3}],
    }


def _wz(wz_id: int, number: str) -> dict:
    return {"id": wz_id, "number": number, "issue_date": "2026-06-10"}


class _Fetches:
    """Patches the four audit fetches with fixed data."""

    def __init__(self, *, invoices, wz_docs, inv_by_wz, doc_actions=None):
        self.invoices = invoices
        self.wz_docs = wz_docs
        self.inv_by_wz = inv_by_wz
        self.doc_actions = doc_actions or {}

    def __enter__(self):
        mod = "zdrovena.month_closing.warehouse_audit"
        self._patches = [
            patch(f"{mod}.fetch_invoices", return_value=self.invoices),
            patch(f"{mod}.fetch_wz_documents", return_value=self.wz_docs),
            patch(f"{mod}.fetch_warehouse_actions", return_value=[]),
            patch(f"{mod}.build_actions_by_doc", return_value=self.doc_actions),
            patch(f"{mod}.build_wz_by_id", return_value={w["id"]: w for w in self.wz_docs}),
            patch(f"{mod}.build_inv_by_wz", return_value=self.inv_by_wz),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()
        return False


class TestWarehouseIssues:
    def test_a_consistent_month_raises_nothing(self):
        inv, wz = _invoice(1, "1/2026"), _wz(9, "WZ 1/2026")
        with _Fetches(invoices=[inv], wz_docs=[wz], inv_by_wz={9: inv}):
            assert warehouse_issues(MagicMock(), 2026, 6) == []

    def test_an_invoice_without_wz_is_reported(self):
        inv = _invoice(1, "FS 7/2026")
        with _Fetches(invoices=[inv], wz_docs=[], inv_by_wz={}):
            issues = warehouse_issues(MagicMock(), 2026, 6)

        assert len(issues) == 1
        assert issues[0]["id"] == "warehouse-invoices-without-wz"
        assert issues[0]["stage"] == "check"
        assert "FS 7/2026" in issues[0]["message"]

    def test_a_wz_without_an_invoice_is_reported(self):
        wz = _wz(9, "WZ 3/2026")
        with _Fetches(invoices=[], wz_docs=[wz], inv_by_wz={}):
            issues = warehouse_issues(MagicMock(), 2026, 6)

        assert [i["id"] for i in issues] == ["warehouse-orphan-wz"]
        assert "WZ 3/2026" in issues[0]["message"]

    def test_both_problems_are_reported_separately(self):
        """Two different accounting problems; waiving one must not waive the other."""
        inv, wz = _invoice(1, "FS 7/2026"), _wz(9, "WZ 3/2026")
        with _Fetches(invoices=[inv], wz_docs=[wz], inv_by_wz={}):
            ids = {i["id"] for i in warehouse_issues(MagicMock(), 2026, 6)}

        assert ids == {"warehouse-invoices-without-wz", "warehouse-orphan-wz"}

    def test_the_message_does_not_list_every_document(self):
        """A month with 200 problems must not produce an unreadable issue."""
        invoices = [_invoice(i, f"FS {i}/2026") for i in range(40)]
        with _Fetches(invoices=invoices, wz_docs=[], inv_by_wz={}):
            message = warehouse_issues(MagicMock(), 2026, 6)[0]["message"]

        assert "40" in message
        assert len(message) < 400

    def test_a_provider_failure_is_surfaced_not_swallowed(self):
        """Silently returning [] would report a clean warehouse we never checked."""
        mod = "zdrovena.month_closing.warehouse_audit"
        with patch(f"{mod}.fetch_invoices", side_effect=RuntimeError("Fakturownia down")):
            issues = warehouse_issues(MagicMock(), 2026, 6)

        assert len(issues) == 1
        assert issues[0]["id"] == "warehouse-audit-unavailable"
        assert issues[0]["severity"] == "warning"


class TestSeverity:
    def test_the_default_is_a_warning_not_a_blocker(self):
        """#308 conditions `blocker` on counting real months first. Until that
        count exists, defaulting to blocker could wall off every close."""
        assert DEFAULT_SEVERITY == "warning"

    def test_severity_is_configurable(self, monkeypatch):
        monkeypatch.setenv("MONTH_CLOSE_WAREHOUSE_SEVERITY", "blocker")
        assert resolve_severity() == "blocker"

    def test_an_unknown_severity_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.setenv("MONTH_CLOSE_WAREHOUSE_SEVERITY", "catastrophic")
        assert resolve_severity() == DEFAULT_SEVERITY

    def test_the_configured_severity_reaches_the_issues(self, monkeypatch):
        monkeypatch.setenv("MONTH_CLOSE_WAREHOUSE_SEVERITY", "blocker")
        inv = _invoice(1, "FS 7/2026")
        with _Fetches(invoices=[inv], wz_docs=[], inv_by_wz={}):
            issues = warehouse_issues(MagicMock(), 2026, 6)

        assert issues[0]["severity"] == "blocker"


class TestTheInspectorActuallyRunsIt:
    """The point of #308: the check exists but nothing called it."""

    def _inspect(self, warehouse_return):
        from zdrovena.month_closing.workflow import MonthCloseInspector

        client = MagicMock()
        client.fetch_sales_invoices.return_value = [{"id": 1, "number": "1/2026"}]
        client.fetch_cost_invoices.return_value = []
        storage = MagicMock()
        storage.list_files.return_value = []

        with (
            patch(
                "zdrovena.month_closing.workflow.FakturowniaClient.from_keyring",
                return_value=client,
            ),
            patch(
                "zdrovena.month_closing.workflow.warehouse_issues",
                return_value=warehouse_return,
            ) as spy,
        ):
            result = MonthCloseInspector(2026, 6, storage=storage).inspect()
        return result, spy

    def test_the_warehouse_audit_is_invoked_for_the_inspected_month(self):
        _result, spy = self._inspect([])

        spy.assert_called_once()
        assert spy.call_args.args[1:] == (2026, 6)

    def test_warehouse_findings_reach_the_operator_issue_list(self):
        finding = {
            "id": "warehouse-orphan-wz",
            "severity": "warning",
            "message": "WZ bez faktury (1)",
            "stage": "check",
        }
        result, _spy = self._inspect([finding])

        assert finding in result["issues"]

    def test_a_blocking_warehouse_finding_marks_the_month_not_ready(self):
        """The waiver mechanism is what lets an operator go on anyway."""
        finding = {
            "id": "warehouse-invoices-without-wz",
            "severity": "blocker",
            "message": "Faktury bez WZ (3)",
            "stage": "check",
        }
        result, _spy = self._inspect([finding])

        assert result["metrics"]["ready"] is False
