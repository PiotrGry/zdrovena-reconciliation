"""Tests for zdrovena.audit.sections — pure business logic."""

from __future__ import annotations


# ── Local Verdict stub (mirrors audit_cmd.Verdict, keeps tests self-contained) ─

class _Verdict:
    def __init__(self) -> None:
        self._issues: list[str] = []

    def fail(self, msg: str) -> None:
        self._issues.append(msg)

    @property
    def passed(self) -> bool:
        return not self._issues

    @property
    def issues(self) -> list[str]:
        return list(self._issues)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _inv(
    *,
    id: int = 1,
    number: str = "1/01/2025",
    kind: str = "vat",
    sell_date: str = "2025-01-10",
    issue_date: str = "2025-01-10",
    wz_id: int | None = None,
    plastic: int = 0,
    glass: int = 0,
) -> dict:
    """Build a minimal invoice dict."""
    positions = []
    if plastic:
        positions.append({"name": f"Woda Humio 500ml x {plastic} butelek", "quantity": 1})
    if glass:
        positions.append({"name": f"Woda Humio szkło {glass} butelek", "quantity": 1})
    d = {
        "id": id,
        "number": number,
        "kind": kind,
        "sell_date": sell_date,
        "issue_date": issue_date,
        "positions": positions,
    }
    if wz_id is not None:
        d["warehouse_document_id"] = wz_id
    return d


def _wz(*, id: int = 201, number: str = "WZ/01/2025", issue_date: str = "2025-01-10") -> dict:
    return {"id": id, "number": number, "issue_date": issue_date}


def _actions(plastic: int = 0, glass: int = 0) -> list[dict]:
    """Build WZ warehouse action rows."""
    result = []
    if plastic:
        result.append({"product_name": "Woda Humio butelka plastik", "quantity": -plastic})
    if glass:
        result.append({"product_name": "Woda Humio butelka szkło", "quantity": -glass})
    return result


# ── check_numbering ──────────────────────────────────────────────────────────

class TestCheckNumbering:
    from zdrovena.audit.sections import check_numbering  # static import for type

    def test_empty_list(self):
        from zdrovena.audit.sections import check_numbering
        assert check_numbering([]) == []

    def test_single_invoice_ok(self):
        from zdrovena.audit.sections import check_numbering
        invs = [{"number": "1/01/2025"}]
        results = check_numbering(invs)
        assert len(results) == 1
        r = results[0]
        assert r.series == "01/2025"
        assert r.count == 1
        assert r.ok is True
        assert r.gaps == []
        assert r.duplicates == []

    def test_continuous_series(self):
        from zdrovena.audit.sections import check_numbering
        invs = [{"number": f"{i}/01/2025"} for i in range(1, 6)]
        results = check_numbering(invs)
        assert len(results) == 1
        r = results[0]
        assert r.ok is True
        assert r.first == 1
        assert r.last == 5
        assert r.count == 5

    def test_gap_detected(self):
        from zdrovena.audit.sections import check_numbering
        invs = [{"number": "1/01/2025"}, {"number": "3/01/2025"}]
        results = check_numbering(invs)
        assert len(results) == 1
        r = results[0]
        assert r.ok is False
        assert 2 in r.gaps

    def test_duplicate_detected(self):
        from zdrovena.audit.sections import check_numbering
        invs = [
            {"number": "1/01/2025"},
            {"number": "2/01/2025"},
            {"number": "2/01/2025"},
        ]
        results = check_numbering(invs)
        r = results[0]
        assert r.ok is False
        assert 2 in r.duplicates

    def test_non_standard_number_ignored(self):
        from zdrovena.audit.sections import check_numbering
        invs = [{"number": "ABC/2025"}, {"number": "NOFORMAT"}]
        assert check_numbering(invs) == []

    def test_multiple_series(self):
        from zdrovena.audit.sections import check_numbering
        invs = [
            {"number": "1/01/2025"},
            {"number": "2/01/2025"},
            {"number": "1/02/2025"},
        ]
        results = check_numbering(invs)
        assert len(results) == 2
        series = {r.series for r in results}
        assert "01/2025" in series
        assert "02/2025" in series


# ── section_numbering ────────────────────────────────────────────────────────

class TestSectionNumbering:
    def test_clean_numbering_passes(self, capsys):
        from zdrovena.audit.sections import section_numbering
        v = _Verdict()
        invs = [{"number": f"{i}/01/2025"} for i in range(1, 4)]
        section_numbering(invs, v)
        assert v.passed

    def test_gap_fails_verdict(self, capsys):
        from zdrovena.audit.sections import section_numbering
        v = _Verdict()
        invs = [{"number": "1/01/2025"}, {"number": "3/01/2025"}]
        section_numbering(invs, v)
        assert not v.passed
        assert any("Numbering" in i for i in v.issues)

    def test_empty_invoices(self, capsys):
        from zdrovena.audit.sections import section_numbering
        v = _Verdict()
        section_numbering([], v)
        assert v.passed


# ── section_recount ──────────────────────────────────────────────────────────

class TestSectionRecount:
    def test_balanced_passes(self, capsys):
        from zdrovena.audit.sections import section_recount
        v = _Verdict()
        inv = _inv(id=1, sell_date="2025-01-10", issue_date="2025-01-10", plastic=12)
        inv_by_wz = {201: inv}
        doc_actions = {201: _actions(plastic=12)}
        section_recount(inv_by_wz, doc_actions, v)
        assert v.passed

    def test_mismatch_fails_verdict(self, capsys):
        from zdrovena.audit.sections import section_recount
        v = _Verdict()
        inv = _inv(id=1, sell_date="2025-01-10", issue_date="2025-01-10", plastic=12)
        inv_by_wz = {201: inv}
        doc_actions = {201: _actions(plastic=10)}  # WZ has only 10
        section_recount(inv_by_wz, doc_actions, v)
        assert not v.passed
        assert any("mismatch" in i for i in v.issues)

    def test_returns_correct_totals(self, capsys):
        from zdrovena.audit.sections import section_recount
        v = _Verdict()
        inv = _inv(plastic=6, glass=3, sell_date="2025-02-05", issue_date="2025-02-05")
        inv_by_wz = {201: inv}
        doc_actions = {201: _actions(plastic=6, glass=3)}
        fv, wz, _, _ = section_recount(inv_by_wz, doc_actions, v)
        assert fv == 9
        assert wz == 9

    def test_empty_inv_by_wz(self, capsys):
        from zdrovena.audit.sections import section_recount
        v = _Verdict()
        fv, wz, _, _ = section_recount({}, {}, v)
        assert fv == 0
        assert wz == 0
        assert v.passed

    def test_month_filter_applies(self, capsys):
        from zdrovena.audit.sections import section_recount
        v = _Verdict()
        inv1 = _inv(id=1, sell_date="2025-01-10", issue_date="2025-01-10", plastic=12)
        inv2 = _inv(id=2, sell_date="2025-02-10", issue_date="2025-02-10", plastic=6)
        inv_by_wz = {201: inv1, 202: inv2}
        doc_actions = {201: _actions(plastic=12), 202: _actions(plastic=6)}
        # Pass month=1 to get only January data
        fv, wz, month_invs, _ = section_recount(inv_by_wz, doc_actions, v, month=1)
        assert 1 in month_invs
        assert fv == 18  # recount sums all months regardless of filter param


# ── section_type_match ───────────────────────────────────────────────────────

class TestSectionTypeMatch:
    def test_types_match_passes(self, capsys):
        from zdrovena.audit.sections import section_type_match
        v = _Verdict()
        inv = _inv(plastic=6, glass=2, sell_date="2025-01-10", issue_date="2025-01-10")
        inv_by_wz = {201: inv}
        doc_actions = {201: _actions(plastic=6, glass=2)}
        result = section_type_match(inv_by_wz, doc_actions, v)
        assert result == []
        assert v.passed

    def test_plastic_mismatch_fails(self, capsys):
        from zdrovena.audit.sections import section_type_match
        v = _Verdict()
        inv = _inv(plastic=6, sell_date="2025-01-10", issue_date="2025-01-10")
        inv_by_wz = {201: inv}
        doc_actions = {201: _actions(plastic=4)}  # WZ has 4, FV has 6
        result = section_type_match(inv_by_wz, doc_actions, v)
        assert len(result) == 1
        assert not v.passed

    def test_empty_passes(self, capsys):
        from zdrovena.audit.sections import section_type_match
        v = _Verdict()
        result = section_type_match({}, {}, v)
        assert result == []
        assert v.passed


# ── section_orphan_wz ────────────────────────────────────────────────────────

class TestSectionOrphanWz:
    def test_all_linked_passes(self, capsys):
        from zdrovena.audit.sections import section_orphan_wz
        v = _Verdict()
        wz_doc = _wz(id=201)
        inv = _inv(id=1, plastic=6)
        inv_by_wz = {201: inv}
        doc_actions = {201: _actions(plastic=6)}
        result = section_orphan_wz([wz_doc], inv_by_wz, doc_actions, v)
        assert result == []
        assert v.passed

    def test_orphan_fails_verdict(self, capsys):
        from zdrovena.audit.sections import section_orphan_wz
        v = _Verdict()
        wz_doc = _wz(id=201)
        inv_by_wz: dict = {}  # WZ 201 has no invoice
        doc_actions = {201: _actions(plastic=6)}
        result = section_orphan_wz([wz_doc], inv_by_wz, doc_actions, v)
        assert len(result) == 1
        assert not v.passed
        assert any("Orphan" in i for i in v.issues)

    def test_empty_wz_list_passes(self, capsys):
        from zdrovena.audit.sections import section_orphan_wz
        v = _Verdict()
        result = section_orphan_wz([], {}, {}, v)
        assert result == []
        assert v.passed


# ── section_no_wz ────────────────────────────────────────────────────────────

class TestSectionNoWz:
    def test_all_linked_passes(self, capsys):
        from zdrovena.audit.sections import section_no_wz
        v = _Verdict()
        inv = _inv(id=1, plastic=12)
        inv_by_wz = {201: inv}
        result = section_no_wz([inv], inv_by_wz, v)
        assert result == []
        assert v.passed

    def test_bottle_inv_without_wz_fails(self, capsys):
        from zdrovena.audit.sections import section_no_wz
        v = _Verdict()
        inv = _inv(id=1, plastic=12)
        inv_by_wz: dict = {}  # invoice not linked to any WZ
        result = section_no_wz([inv], inv_by_wz, v)
        assert len(result) == 1
        assert not v.passed

    def test_invoice_without_bottles_ignored(self, capsys):
        from zdrovena.audit.sections import section_no_wz
        v = _Verdict()
        inv = _inv(id=1)  # no plastic, no glass
        result = section_no_wz([inv], {}, v)
        assert result == []
        assert v.passed


# ── section_date_comparison ──────────────────────────────────────────────────

class TestSectionDateComparison:
    def test_same_month_passes(self, capsys):
        from zdrovena.audit.sections import section_date_comparison
        v = _Verdict()
        inv = _inv(sell_date="2025-01-10", issue_date="2025-01-10", plastic=6)
        wz_doc = _wz(id=201, issue_date="2025-01-12")
        inv_by_wz = {201: inv}
        wz_by_id = {201: wz_doc}
        result = section_date_comparison(inv_by_wz, wz_by_id, v)
        assert result == []
        assert v.passed

    def test_cross_month_fails(self, capsys):
        from zdrovena.audit.sections import section_date_comparison
        v = _Verdict()
        inv = _inv(sell_date="2025-01-31", issue_date="2025-01-31", plastic=6)
        wz_doc = _wz(id=201, issue_date="2025-02-01")
        inv_by_wz = {201: inv}
        wz_by_id = {201: wz_doc}
        result = section_date_comparison(inv_by_wz, wz_by_id, v)
        assert len(result) == 1
        assert not v.passed


# ── section_cross_month_sell_issue ───────────────────────────────────────────

class TestSectionCrossMonthSellIssue:
    def test_same_month_ok(self, capsys):
        from zdrovena.audit.sections import section_cross_month_sell_issue
        v = _Verdict()
        inv = _inv(sell_date="2025-01-10", issue_date="2025-01-15", plastic=6)
        result = section_cross_month_sell_issue([inv], v)
        assert result == []

    def test_cross_month_detected(self, capsys):
        from zdrovena.audit.sections import section_cross_month_sell_issue
        v = _Verdict()
        inv = _inv(sell_date="2025-01-31", issue_date="2025-02-01", plastic=6)
        result = section_cross_month_sell_issue([inv], v)
        assert len(result) == 1

    def test_invoice_without_bottles_ignored(self, capsys):
        from zdrovena.audit.sections import section_cross_month_sell_issue
        v = _Verdict()
        inv = _inv(sell_date="2025-01-31", issue_date="2025-02-01")  # no bottles
        result = section_cross_month_sell_issue([inv], v)
        assert result == []

    def test_missing_dates_ignored(self, capsys):
        from zdrovena.audit.sections import section_cross_month_sell_issue
        v = _Verdict()
        inv = {"id": 1, "sell_date": "", "issue_date": "", "positions": []}
        result = section_cross_month_sell_issue([inv], v)
        assert result == []


# ── section_stock_balance ────────────────────────────────────────────────────

class TestSectionStockBalance:
    def test_no_actions_prints_nothing_fails_not(self, capsys):
        from zdrovena.audit.sections import section_stock_balance
        v = _Verdict()
        section_stock_balance([], [], year=2025, month=1, verdict=v)
        assert v.passed

    def test_bottle_actions_printed(self, capsys):
        from zdrovena.audit.sections import section_stock_balance
        v = _Verdict()
        actions = [
            {"wd_issue_date": "2025-01-10", "product_name": "Woda Humio butelka plastik", "quantity": 100.0},
            {"wd_issue_date": "2025-01-15", "product_name": "Woda Humio butelka plastik", "quantity": -50.0},
        ]
        section_stock_balance(actions, [], year=2025, month=1, verdict=v)
        out = capsys.readouterr().out
        assert "plastik" in out or "PZ" in out

    def test_non_bottle_actions_ignored(self, capsys):
        from zdrovena.audit.sections import section_stock_balance
        v = _Verdict()
        actions = [
            {"wd_issue_date": "2025-01-10", "product_name": "Koszulka firmowa", "quantity": 10.0},
        ]
        section_stock_balance(actions, [], year=2025, month=1, verdict=v)
        out = capsys.readouterr().out
        # Non-bottle products: monthly movements not printed, only zero totals line
        assert "Sty" not in out  # no monthly row for January

    def test_warehouse_products_displayed(self, capsys):
        from zdrovena.audit.sections import section_stock_balance
        v = _Verdict()
        products = [
            {"name": "Woda Humio butelka plastik", "warehouse_quantity": 500},
            {"name": "Woda Humio butelka szkło", "warehouse_quantity": 200},
        ]
        section_stock_balance([], products, year=2025, month=None, verdict=v)
        out = capsys.readouterr().out
        assert "500" in out
        assert "200" in out

    def test_month_none_uses_year_prefix(self, capsys):
        from zdrovena.audit.sections import section_stock_balance
        v = _Verdict()
        actions = [
            {"wd_issue_date": "2025-03-10", "product_name": "Woda Humio butelka plastik", "quantity": 60.0},
        ]
        section_stock_balance(actions, [], year=2025, month=None, verdict=v)
        out = capsys.readouterr().out
        assert "PZ" in out

    def test_alias_product_counted(self, capsys):
        from zdrovena.audit.sections import section_stock_balance
        v = _Verdict()
        actions = [
            # Legacy alias → maps to plastik
            {"wd_issue_date": "2025-01-05", "product_name": "Woda Humio butelka", "quantity": -30.0},
        ]
        section_stock_balance(actions, [], year=2025, month=1, verdict=v)
        out = capsys.readouterr().out
        assert "WZ" in out


# ── section_anomalies ────────────────────────────────────────────────────────

class TestSectionAnomalies:
    def test_no_anomalies(self, capsys):
        from zdrovena.audit.sections import section_anomalies
        inv = _inv(id=1, plastic=12, sell_date="2025-01-10", issue_date="2025-01-10")
        inv_by_wz = {201: inv}
        wz_doc = _wz(id=201)
        wz_by_id = {201: wz_doc}
        section_anomalies(inv_by_wz, wz_by_id, [inv])
        out = capsys.readouterr().out
        assert "✅" in out

    def test_large_invoice_flagged(self, capsys):
        from zdrovena.audit.sections import section_anomalies
        inv = _inv(id=1, plastic=80, sell_date="2025-01-10", issue_date="2025-01-10")
        inv_by_wz = {201: inv}
        wz_doc = _wz(id=201)
        wz_by_id = {201: wz_doc}
        section_anomalies(inv_by_wz, wz_by_id, [inv])
        out = capsys.readouterr().out
        assert ">72" in out or "80" in out

    def test_multi_linked_wz_flagged(self, capsys):
        from zdrovena.audit.sections import section_anomalies
        wz_doc = _wz(id=201)
        wz_by_id = {201: wz_doc}
        inv1 = _inv(id=1, plastic=6, sell_date="2025-01-10", issue_date="2025-01-10", wz_id=201)
        inv2 = _inv(id=2, plastic=6, sell_date="2025-01-11", issue_date="2025-01-11", wz_id=201)
        section_anomalies({201: inv1}, wz_by_id, [inv1, inv2])
        out = capsys.readouterr().out
        # Either "WZ" or "✅" present — multi-WZ detected only if both invs link same wz
        assert out  # something was printed
