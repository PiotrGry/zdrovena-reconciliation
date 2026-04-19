"""Tests for zdrovena.audit.api (pure helpers — no HTTP)."""

from __future__ import annotations

from zdrovena.audit.api import (
    build_actions_by_doc,
    build_inv_by_wz,
    build_wz_by_id,
    date_range,
    doc_type_label,
    inv_sort_key,
    is_receipt,
    month_of,
    sell_date_of,
)

# ── date_range ────────────────────────────────────────────────────────────────


class TestDateRange:
    def test_full_year(self):
        assert date_range(2025) == ("2025-01-01", "2025-12-31")

    def test_month(self):
        assert date_range(2025, 2) == ("2025-02-01", "2025-02-28")

    def test_month_leap_year(self):
        assert date_range(2024, 2) == ("2024-02-01", "2024-02-29")

    def test_december(self):
        assert date_range(2025, 12) == ("2025-12-01", "2025-12-31")

    def test_single_day(self):
        assert date_range(2025, 6, 15) == ("2025-06-15", "2025-06-15")

    def test_month_january(self):
        assert date_range(2025, 1) == ("2025-01-01", "2025-01-31")

    def test_month_end_30_days(self):
        assert date_range(2025, 4) == ("2025-04-01", "2025-04-30")


# ── month_of ──────────────────────────────────────────────────────────────────


class TestMonthOf:
    def test_normal(self):
        assert month_of("2025-06-15") == 6

    def test_january(self):
        assert month_of("2025-01-01") == 1

    def test_december(self):
        assert month_of("2025-12-31") == 12

    def test_empty_string(self):
        assert month_of("") == 0


# ── sell_date_of ──────────────────────────────────────────────────────────────


class TestSellDateOf:
    def test_has_sell_date(self):
        inv = {"sell_date": "2025-02-10", "issue_date": "2025-02-11"}
        assert sell_date_of(inv) == "2025-02-10"

    def test_falls_back_to_issue_date(self):
        inv = {"sell_date": "", "issue_date": "2025-02-11"}
        assert sell_date_of(inv) == "2025-02-11"

    def test_no_sell_date_key(self):
        inv = {"issue_date": "2025-02-11"}
        assert sell_date_of(inv) == "2025-02-11"

    def test_no_dates(self):
        inv = {}
        assert sell_date_of(inv) == ""


# ── is_receipt / doc_type_label ───────────────────────────────────────────────


class TestDocType:
    def test_is_receipt_true(self):
        assert is_receipt({"kind": "receipt"}) is True

    def test_is_receipt_false(self):
        assert is_receipt({"kind": "vat"}) is False

    def test_doc_type_label_receipt(self):
        assert doc_type_label({"kind": "receipt"}) == "PAR"

    def test_doc_type_label_vat(self):
        assert doc_type_label({"kind": "vat"}) == "FV"


# ── inv_sort_key ──────────────────────────────────────────────────────────────


class TestInvSortKey:
    def test_normal_number(self):
        assert inv_sort_key({"number": "12/02/2025"}) == (12, "12/02/2025")

    def test_no_slash(self):
        key = inv_sort_key({"number": "PROFORMA-1"})
        assert key[0] == 999_999

    def test_sorting(self):
        invoices = [
            {"number": "3/02/2025"},
            {"number": "1/02/2025"},
            {"number": "10/02/2025"},
        ]
        sorted_invs = sorted(invoices, key=inv_sort_key)
        assert [i["number"] for i in sorted_invs] == [
            "1/02/2025",
            "3/02/2025",
            "10/02/2025",
        ]


# ── build_wz_by_id ───────────────────────────────────────────────────────────


class TestBuildWzById:
    def test_maps_by_id(self):
        docs = [{"id": 1, "number": "WZ1"}, {"id": 2, "number": "WZ2"}]
        result = build_wz_by_id(docs)
        assert result == {1: docs[0], 2: docs[1]}

    def test_empty_list(self):
        assert build_wz_by_id([]) == {}


# ── build_actions_by_doc ──────────────────────────────────────────────────────


class TestBuildActionsByDoc:
    def test_groups_by_doc_id(self):
        actions = [
            {"warehouse_document_id": 1, "product_name": "A"},
            {"warehouse_document_id": 1, "product_name": "B"},
            {"warehouse_document_id": 2, "product_name": "C"},
        ]
        result = build_actions_by_doc(actions)
        assert len(result[1]) == 2
        assert len(result[2]) == 1

    def test_empty_list(self):
        assert build_actions_by_doc([]) == {}


# ── build_inv_by_wz ──────────────────────────────────────────────────────────


class TestBuildInvByWz:
    def test_links_invoices_to_wz(self):
        invoices = [
            {"id": 10, "warehouse_document_id": 100},
            {"id": 11, "warehouse_document_id": 200},
            {"id": 12},  # no WZ link
        ]
        wz_by_id = {100: {"id": 100}, 200: {"id": 200}}

        result = build_inv_by_wz(invoices, wz_by_id)
        assert 100 in result
        assert 200 in result
        assert result[100]["id"] == 10

    def test_ignores_unknown_wz(self):
        invoices = [{"id": 10, "warehouse_document_id": 999}]
        wz_by_id = {100: {"id": 100}}
        assert build_inv_by_wz(invoices, wz_by_id) == {}
