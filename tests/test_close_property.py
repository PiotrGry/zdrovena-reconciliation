"""Property-based tests for core close-month logic.

Tests invariants that must hold for all valid inputs:
- CloseRequest validation bounds
- Vendor pattern matching (case-insensitive substring)
- Date range arithmetic
- CloseReport.has_critical_errors property
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from zdrovena.api.models import CloseRequest
from zdrovena.month_closing.orchestrator import CloseReport

# ─── CloseRequest validation ──────────────────────────────────────────────────


@given(st.integers(min_value=2020, max_value=2030), st.integers(min_value=1, max_value=12))
def test_closerequest_accepts_all_valid_month_year_combos(year: int, month: int) -> None:
    req = CloseRequest(year=year, month=month)
    assert req.year == year
    assert req.month == month


@given(st.integers(min_value=2020, max_value=2030), st.integers().filter(lambda m: not (1 <= m <= 12)))
def test_closerequest_rejects_invalid_month(year: int, month: int) -> None:
    with pytest.raises(ValidationError):
        CloseRequest(year=year, month=month)


@given(st.integers(max_value=2019), st.integers(min_value=1, max_value=12))
def test_closerequest_rejects_year_before_2020(year: int, month: int) -> None:
    with pytest.raises(ValidationError):
        CloseRequest(year=year, month=month)


# ─── Vendor pattern matching ──────────────────────────────────────────────────
# Replicates the exact matching logic from orchestrator._step_4_cost_invoices:
#   pat = vendor_cfg.pattern.lower()
#   if pat in buyer.lower() or pat in buyer_nip.lower(): ...


def _matches(pattern: str, buyer_name: str, buyer_nip: str) -> bool:
    pat = pattern.lower()
    return pat in (buyer_name or "").lower() or pat in (buyer_nip or "").lower()


@given(
    st.text(min_size=1, max_size=30, alphabet=st.characters(blacklist_categories=("Cs",))),
    st.text(max_size=100, alphabet=st.characters(blacklist_categories=("Cs",))),
    st.text(max_size=20, alphabet=st.characters(blacklist_categories=("Cs",))),
)
def test_vendor_match_is_case_insensitive(pattern: str, buyer_name: str, buyer_nip: str) -> None:
    assert _matches(pattern, buyer_name, buyer_nip) == _matches(
        pattern.upper(), buyer_name.lower(), buyer_nip.lower()
    )


@given(
    st.text(min_size=1, max_size=30, alphabet=st.characters(blacklist_categories=("Cs",))),
)
def test_vendor_pattern_matches_itself_as_buyer(pattern: str) -> None:
    assert _matches(pattern, pattern, "")


@given(
    st.text(min_size=1, max_size=30, alphabet=st.characters(blacklist_categories=("Cs",))),
    st.text(min_size=1, max_size=30, alphabet=st.characters(blacklist_categories=("Cs",))),
)
def test_vendor_pattern_matches_when_embedded_in_buyer(pattern: str, suffix: str) -> None:
    assert _matches(pattern, pattern + suffix, "")
    assert _matches(pattern, suffix + pattern, "")


@given(
    st.text(min_size=2, max_size=10, alphabet=st.characters(blacklist_categories=("Cs",))),
)
def test_vendor_no_match_when_buyer_and_nip_empty(pattern: str) -> None:
    assert not _matches(pattern, "", "")


# ─── Date range arithmetic ────────────────────────────────────────────────────


@given(
    st.integers(min_value=2020, max_value=2030),
    st.integers(min_value=1, max_value=12),
)
def test_date_range_covers_full_calendar_month(year: int, month: int) -> None:
    date_from = date(year, month, 1)
    last_day = calendar.monthrange(year, month)[1]
    date_to = date(year, month, last_day)

    assert date_from.day == 1
    assert date_to.day == last_day
    assert date_from <= date_to
    # Every day in the month is between date_from and date_to
    for day in range(1, last_day + 1):
        d = date(year, month, day)
        assert date_from <= d <= date_to


@given(
    st.integers(min_value=2020, max_value=2030),
    st.integers(min_value=1, max_value=12),
    st.integers(min_value=0, max_value=30),
)
def test_cost_date_range_overlap_extends_by_exact_days(
    year: int, month: int, overlap_days: int
) -> None:
    last_day = calendar.monthrange(year, month)[1]
    date_to = date(year, month, last_day)
    cost_date_to = date_to + timedelta(days=overlap_days)

    assert cost_date_to >= date_to
    assert (cost_date_to - date_to).days == overlap_days


# ─── CloseReport invariants ───────────────────────────────────────────────────


@given(st.lists(st.text(min_size=1, max_size=80), max_size=10))
def test_has_critical_errors_iff_errors_nonempty(errors: list[str]) -> None:
    report = CloseReport()
    report.errors = errors
    assert report.has_critical_errors == (len(errors) > 0)


@given(st.lists(st.text(min_size=1, max_size=80), max_size=10))
def test_warnings_do_not_set_critical_errors(warnings: list[str]) -> None:
    report = CloseReport()
    report.warnings = warnings
    assert not report.has_critical_errors


@given(
    st.lists(st.text(min_size=1, max_size=40), max_size=5),
    st.lists(st.text(min_size=1, max_size=40), max_size=5),
)
def test_has_critical_errors_driven_only_by_errors_field(
    warnings: list[str], errors: list[str]
) -> None:
    report = CloseReport()
    report.warnings = warnings
    report.errors = errors
    assert report.has_critical_errors == (len(errors) > 0)
