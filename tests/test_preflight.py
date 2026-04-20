"""Tests for zdrovena.month_closing.preflight."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from zdrovena.month_closing.preflight import (
    PreflightChecker,
    PreflightResult,
    pko_matches_month,
)

# ── pko_matches_month ─────────────────────────────────────────────────────────


class TestPkoMatchesMonth:
    def test_correct_next_month(self):
        # Bank statement for June 2025 should show up in July filename
        assert pko_matches_month("Wyciag_na_zadanie_20250701001.pdf", 2025, 6) is True

    def test_correct_next_month_december(self):
        # December → January next year
        assert pko_matches_month("Wyciag_na_zadanie_20260101001.pdf", 2025, 12) is True

    def test_wrong_month(self):
        assert pko_matches_month("Wyciag_na_zadanie_20250601001.pdf", 2025, 6) is False

    def test_no_match_pattern(self):
        assert pko_matches_month("random_file.pdf", 2025, 6) is False

    def test_invalid_numbers(self):
        assert pko_matches_month("Wyciag_na_zadanie_202X0701.pdf", 2025, 6) is False


# ── PreflightResult defaults ──────────────────────────────────────────────────


class TestPreflightResult:
    def test_defaults(self):
        r = PreflightResult()
        assert r.matches == []
        assert r.missing_vendors == []
        assert r.missing_reports == []
        assert r.bank_statement_found is False
        assert r.warnings == []


# ── PreflightChecker.build_blockers ──────────────────────────────────────────


def _make_checker(tmp_path: Path) -> PreflightChecker:
    return PreflightChecker(
        year=2025,
        month=6,
        month_dir=tmp_path / "month",
        date_from="2025-06-01",
        date_to="2025-06-30",
        cost_date_to="2025-07-15",
        dry_run=True,
        get_secret=MagicMock(return_value=None),
    )


class TestBuildBlockers:
    def test_no_blockers_when_all_found(self, tmp_path):
        checker = _make_checker(tmp_path)
        checker.result.bank_statement_found = True
        assert checker.build_blockers() == []

    def test_missing_bank_statement(self, tmp_path):
        checker = _make_checker(tmp_path)
        checker.result.bank_statement_found = False
        blockers = checker.build_blockers()
        assert any("PKO BP" in b for b in blockers)

    def test_missing_vendor(self, tmp_path):
        from zdrovena.month_closing.config import VendorConfig

        checker = _make_checker(tmp_path)
        checker.result.bank_statement_found = True
        vendor = VendorConfig(name="TestVendor", pattern="testvendor")
        checker.result.missing_vendors.append(vendor)
        blockers = checker.build_blockers()
        assert any("TestVendor" in b for b in blockers)

    def test_missing_vendor_with_url(self, tmp_path):
        from zdrovena.month_closing.config import VendorConfig

        checker = _make_checker(tmp_path)
        checker.result.bank_statement_found = True
        vendor = VendorConfig(
            name="TestVendor", pattern="testvendor", fallback_url="https://example.com"
        )
        checker.result.missing_vendors.append(vendor)
        blockers = checker.build_blockers()
        assert any("https://example.com" in b for b in blockers)


# ── PreflightChecker.copy_to_folders ─────────────────────────────────────────


class TestCopyToFolders:
    def test_copy_cost_vendor(self, tmp_path):
        from zdrovena.month_closing.config import VendorConfig

        checker = _make_checker(tmp_path)
        checker.dry_run = False  # actually copy

        src = tmp_path / "vendor_invoice.pdf"
        src.write_bytes(b"%PDF")
        month_dir = tmp_path / "month"
        costs_dir = tmp_path / "costs"
        month_dir.mkdir()
        costs_dir.mkdir()

        vendor = VendorConfig(name="TestVendor", pattern="testvendor")
        checker.result.matches.append((vendor, src))
        checker.copy_to_folders(month_dir, costs_dir)

        # File should be copied to costs_dir
        dest = costs_dir / f"TestVendor_{src.name}"
        assert dest.exists()

    def test_copy_dry_run_does_not_copy(self, tmp_path):
        from zdrovena.month_closing.config import VendorConfig

        checker = _make_checker(tmp_path)
        checker.dry_run = True

        src = tmp_path / "invoice.pdf"
        src.write_bytes(b"%PDF")
        month_dir = tmp_path / "month"
        costs_dir = tmp_path / "costs"
        month_dir.mkdir()
        costs_dir.mkdir()

        vendor = VendorConfig(name="TestVendor", pattern="testvendor")
        checker.result.matches.append((vendor, src))
        checker.copy_to_folders(month_dir, costs_dir)

        dest = costs_dir / f"TestVendor_{src.name}"
        assert not dest.exists()

    def test_skip_existing_file(self, tmp_path):
        from zdrovena.month_closing.config import VendorConfig

        checker = _make_checker(tmp_path)
        checker.dry_run = False

        src = tmp_path / "invoice.pdf"
        src.write_bytes(b"%PDF original")
        month_dir = tmp_path / "month"
        costs_dir = tmp_path / "costs"
        month_dir.mkdir()
        costs_dir.mkdir()

        # Pre-create the dest file
        dest = costs_dir / f"TestVendor_{src.name}"
        dest.write_bytes(b"%PDF existing")

        vendor = VendorConfig(name="TestVendor", pattern="testvendor")
        checker.result.matches.append((vendor, src))
        checker.copy_to_folders(month_dir, costs_dir)

        # Dest should not be overwritten
        assert dest.read_bytes() == b"%PDF existing"


# ── _check_bank_statement ─────────────────────────────────────────────────────


class TestCheckBankStatement:
    def test_found_in_month_dir(self, tmp_path):
        checker = _make_checker(tmp_path)
        checker.month_dir = tmp_path
        (tmp_path / "wyciag_czerwiec.pdf").write_bytes(b"%PDF")
        checker._check_bank_statement()
        assert checker.result.bank_statement_found is True

    def test_found_in_month_dir_pko_name(self, tmp_path):
        checker = _make_checker(tmp_path)
        checker.month_dir = tmp_path
        (tmp_path / "pko_2025-06.pdf").write_bytes(b"%PDF")
        checker._check_bank_statement()
        assert checker.result.bank_statement_found is True

    def test_not_found_generates_warning(self, tmp_path):
        checker = _make_checker(tmp_path)
        checker.month_dir = tmp_path / "nonexistent"
        with patch(
            "zdrovena.month_closing.preflight.DOWNLOAD_WATCH_DIR",
            tmp_path / "nonexistent_inbox",
        ):
            checker._check_bank_statement()
        assert checker.result.bank_statement_found is False
        assert checker.result.warnings  # should have a warning


# ── _check_vendors — watch dir missing ───────────────────────────────────────


class TestCheckVendors:
    def test_watch_dir_missing_marks_all_vendors_missing(self, tmp_path):
        from zdrovena.month_closing.config import VendorConfig

        checker = _make_checker(tmp_path)

        vendor = VendorConfig(name="TestVendor", pattern="testvendor", download_glob="*.pdf")
        with patch(
            "zdrovena.month_closing.preflight.DOWNLOAD_WATCH_DIR",
            tmp_path / "nonexistent",
        ):
            checker._check_vendors([vendor])

        assert any(v.name == "TestVendor" for v in checker.result.missing_vendors)

    def test_glob_match_marks_vendor_found(self, tmp_path):
        from zdrovena.month_closing.config import VendorConfig

        checker = _make_checker(tmp_path)
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        (inbox / "vendor_invoice.pdf").write_bytes(b"%PDF")

        vendor = VendorConfig(
            name="TestVendor", pattern="testvendor", download_glob="vendor_invoice.pdf"
        )
        with patch("zdrovena.month_closing.preflight.DOWNLOAD_WATCH_DIR", inbox):
            checker._check_vendors([vendor])

        assert any(v.name == "TestVendor" for v, _ in checker.result.matches)
        assert not any(v.name == "TestVendor" for v in checker.result.missing_vendors)
