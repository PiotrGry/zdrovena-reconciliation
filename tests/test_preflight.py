"""Tests for zdrovena.month_closing.preflight."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from zdrovena.month_closing.preflight import (
    PreflightChecker,
    PreflightResult,
    is_bank_statement_pdf,
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


class TestPkoMatchesMonthCurrentFormat:
    """``Wyciag_<month>_…_<yyyymmdd><n>.pdf`` — the name PKO uses now (hotfix #480).

    Unlike the on-demand format, the date here is inside the statement month,
    so all three of statement month, year and date month must agree.
    """

    def test_matching_month_year_and_date(self):
        assert pko_matches_month("Wyciag_8_12345678_202608311.pdf", 2026, 8) is True

    def test_zero_padded_statement_month(self):
        assert pko_matches_month("Wyciag_08_12345678_202608311.pdf", 2026, 8) is True

    def test_case_insensitive_name(self):
        assert pko_matches_month("WYCIAG_8_12345678_202608311.PDF", 2026, 8) is True

    def test_statement_month_differs(self):
        assert pko_matches_month("Wyciag_7_12345678_202608311.pdf", 2026, 8) is False

    def test_year_differs(self):
        assert pko_matches_month("Wyciag_8_12345678_202508311.pdf", 2026, 8) is False

    def test_date_month_differs(self):
        # The on-demand rule would accept a next-month date; this format must not.
        assert pko_matches_month("Wyciag_8_12345678_202609011.pdf", 2026, 8) is False

    def test_other_period_requested(self):
        assert pko_matches_month("Wyciag_8_12345678_202608311.pdf", 2026, 9) is False


# ── is_bank_statement_pdf ─────────────────────────────────────────────────────


class TestIsBankStatementPdf:
    """Content decides; the period-scoped inbox supplies the date (hotfix #480)."""

    def _check(self, tmp_path: Path, text: str, name: str) -> bool:
        pdf = tmp_path / "candidate.pdf"
        pdf.write_bytes(b"%PDF-1.4 stub")
        with patch("zdrovena.month_closing.preflight.extract_text", return_value=text):
            return is_bank_statement_pdf(pdf, source_name=name)

    def test_pko_statement_text_is_recognised_under_any_name(self, tmp_path):
        text = "PKO Bank Polski SA\nWyciąg z rachunku nr 8/2026\nSaldo końcowe 1 234,56 PLN"
        assert self._check(tmp_path, text, "historia_sierpien.pdf") is True

    def test_invoice_text_is_not_a_statement(self, tmp_path):
        text = "Faktura VAT nr FV/12/08/2026\nSprzedawca: Dostawca Sp. z o.o.\nDo zapłaty: 123,00"
        assert self._check(tmp_path, text, "faktura_08.pdf") is False


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

    def test_a_report_keeps_its_own_extension_when_it_is_not_the_expected_one(self, tmp_path):
        """Renaming a PDF to JPK_FA.xml would hide the mismatch from every later check."""
        checker = _make_checker(tmp_path)
        checker.dry_run = False
        month_dir = tmp_path / "month"
        costs_dir = tmp_path / "costs"
        month_dir.mkdir()
        costs_dir.mkdir()
        report = {"name": "JPK_FA", "dest_name": "JPK_FA.xml"}
        for source in ("zdrovena-2025-06-jpk_fa.XML", "zdrovena-2025-06-jpk_fa_podglad.pdf"):
            src = tmp_path / source
            src.write_bytes(b"x")
            checker.result.matches.append((report, src))

        checker.copy_to_folders(month_dir, costs_dir)

        assert sorted(f.name for f in month_dir.iterdir()) == ["JPK_FA.pdf", "JPK_FA.xml"]


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


# ── Blob storage fallback tests ───────────────────────────────────────────────


def _make_blob_file(key: str, size: int = 1000):
    bf = MagicMock()
    bf.key = key
    bf.size = size
    bf.last_modified = datetime(2025, 6, 15, tzinfo=timezone.utc)
    return bf


def _make_checker_with_storage(tmp_path: Path, storage: MagicMock) -> PreflightChecker:
    return PreflightChecker(
        year=2025,
        month=6,
        month_dir=tmp_path / "month",
        date_from="2025-06-01",
        date_to="2025-06-30",
        cost_date_to="2025-07-15",
        dry_run=True,
        get_secret=MagicMock(return_value=None),
        storage=storage,
    )


class TestBlobFallbackVendors:
    def test_vendor_found_in_blob_via_glob(self, tmp_path):
        from zdrovena.month_closing.config import VendorConfig

        storage = MagicMock()
        blob = _make_blob_file("faktury/inbox/vendor_invoice.pdf")
        storage.list_files.return_value = [blob]
        storage.download.side_effect = lambda key, path: path.write_bytes(b"%PDF")

        checker = _make_checker_with_storage(tmp_path, storage)
        vendor = VendorConfig(
            name="TestVendor", pattern="testvendor", download_glob="vendor_invoice.pdf"
        )
        with patch("zdrovena.month_closing.preflight.DOWNLOAD_WATCH_DIR", tmp_path / "nonexistent"):
            checker._check_vendors([vendor])

        assert any(v.name == "TestVendor" for v, _ in checker.result.matches)
        assert not any(v.name == "TestVendor" for v in checker.result.missing_vendors)

    def test_vendor_not_in_blob_marks_missing(self, tmp_path):
        from zdrovena.month_closing.config import VendorConfig

        storage = MagicMock()
        storage.list_files.return_value = []

        checker = _make_checker_with_storage(tmp_path, storage)
        vendor = VendorConfig(
            name="TestVendor", pattern="testvendor", download_glob="vendor_invoice.pdf"
        )
        with patch("zdrovena.month_closing.preflight.DOWNLOAD_WATCH_DIR", tmp_path / "nonexistent"):
            checker._check_vendors([vendor])

        assert any(v.name == "TestVendor" for v in checker.result.missing_vendors)

    def test_no_storage_and_no_watch_dir_marks_missing(self, tmp_path):
        from zdrovena.month_closing.config import VendorConfig

        checker = _make_checker(tmp_path)  # no storage
        vendor = VendorConfig(
            name="TestVendor", pattern="testvendor", download_glob="vendor_invoice.pdf"
        )
        with patch("zdrovena.month_closing.preflight.DOWNLOAD_WATCH_DIR", tmp_path / "nonexistent"):
            checker._check_vendors([vendor])

        assert any(v.name == "TestVendor" for v in checker.result.missing_vendors)


class TestBlobFallbackBankStatement:
    def test_bank_statement_found_in_blob(self, tmp_path):
        storage = MagicMock()
        # Month 6 → next month is July → filename contains 20250701
        blob = _make_blob_file("faktury/inbox/Wyciag_na_zadanie_20250701001.pdf")
        storage.list_files.return_value = [blob]
        storage.download.side_effect = lambda key, path: path.write_bytes(b"%PDF")

        checker = _make_checker_with_storage(tmp_path, storage)
        checker.month_dir = tmp_path / "nonexistent_month"

        with patch("zdrovena.month_closing.preflight.DOWNLOAD_WATCH_DIR", tmp_path / "nonexistent"):
            checker._check_bank_statement()

        assert checker.result.bank_statement_found is True
        assert any("PKO BP" in str(v) for v, _ in checker.result.matches)

    def test_bank_statement_wrong_month_not_found(self, tmp_path):
        storage = MagicMock()
        # June filename instead of July — wrong month for closing month=6
        blob = _make_blob_file("faktury/inbox/Wyciag_na_zadanie_20250601001.pdf")
        storage.list_files.return_value = [blob]

        checker = _make_checker_with_storage(tmp_path, storage)
        checker.month_dir = tmp_path / "nonexistent_month"

        with patch("zdrovena.month_closing.preflight.DOWNLOAD_WATCH_DIR", tmp_path / "nonexistent"):
            checker._check_bank_statement()

        assert checker.result.bank_statement_found is False


class TestCopyToFoldersBlobMoveSemantics:
    def test_blob_deleted_after_copy(self, tmp_path):
        from zdrovena.month_closing.config import VendorConfig

        storage = MagicMock()
        checker = _make_checker_with_storage(tmp_path, storage)
        checker.dry_run = False

        tmp_file = tmp_path / "tmp_vendor.pdf"
        tmp_file.write_bytes(b"%PDF")
        blob_key = "faktury/inbox/vendor_invoice.pdf"
        checker._blob_downloads = [(blob_key, tmp_file)]

        month_dir = tmp_path / "month"
        costs_dir = tmp_path / "costs"
        month_dir.mkdir()
        costs_dir.mkdir()

        vendor = VendorConfig(name="TestVendor", pattern="testvendor")
        checker.result.matches.append((vendor, tmp_file))
        checker.copy_to_folders(month_dir, costs_dir)

        storage.delete.assert_called_once_with(blob_key)
        assert not tmp_file.exists()

    def test_blob_cleaned_when_dest_already_exists(self, tmp_path):
        from zdrovena.month_closing.config import VendorConfig

        storage = MagicMock()
        checker = _make_checker_with_storage(tmp_path, storage)
        checker.dry_run = False

        tmp_file = tmp_path / "tmp_vendor.pdf"
        tmp_file.write_bytes(b"%PDF")
        blob_key = "faktury/inbox/vendor_invoice.pdf"
        checker._blob_downloads = [(blob_key, tmp_file)]

        month_dir = tmp_path / "month"
        costs_dir = tmp_path / "costs"
        month_dir.mkdir()
        costs_dir.mkdir()

        # Pre-create dest so copy is skipped
        dest = costs_dir / f"TestVendor_{tmp_file.name}"
        dest.write_bytes(b"%PDF existing")

        vendor = VendorConfig(name="TestVendor", pattern="testvendor")
        checker.result.matches.append((vendor, tmp_file))
        checker.copy_to_folders(month_dir, costs_dir)

        # Blob and tmp should be cleaned up even when copy is skipped
        storage.delete.assert_called_once_with(blob_key)
        assert not tmp_file.exists()

    def test_local_file_not_deleted(self, tmp_path):
        from zdrovena.month_closing.config import VendorConfig

        storage = MagicMock()
        checker = _make_checker_with_storage(tmp_path, storage)
        checker.dry_run = False

        # Local file (not in _blob_downloads)
        local_file = tmp_path / "local_invoice.pdf"
        local_file.write_bytes(b"%PDF")

        month_dir = tmp_path / "month"
        costs_dir = tmp_path / "costs"
        month_dir.mkdir()
        costs_dir.mkdir()

        vendor = VendorConfig(name="TestVendor", pattern="testvendor")
        checker.result.matches.append((vendor, local_file))
        checker.copy_to_folders(month_dir, costs_dir)

        storage.delete.assert_not_called()
        assert local_file.exists()


# ── _find_vendor_in_blob — Zoho invoice IDs, glob fallback, misses ───────────

_ZOHO_VENDOR_KW = dict(
    name="ZohoVendor",
    pattern="zohovendor",
    email="billing@zohovendor.example",
    invoice_id_re=r"INV-\d+",
    invoice_file_tpl="zv_{id}.pdf",
    download_glob="zv_*.pdf",
)


def _zoho_vendor(**overrides):
    from zdrovena.month_closing.config import VendorConfig

    return VendorConfig(**{**_ZOHO_VENDOR_KW, **overrides})


def _blob_at(key: str, day: int):
    bf = _make_blob_file(key)
    bf.last_modified = datetime(2025, 6, day, tzinfo=timezone.utc)
    return bf


def _blob_checker(tmp_path: Path, failing_keys: frozenset[str] = frozenset()):
    """Checker over a fake storage; ``download`` raises for ``failing_keys``."""
    storage = MagicMock()
    downloaded: list[str] = []

    def _download(key, path):
        if key in failing_keys:
            raise OSError(f"download of {key} failed")
        downloaded.append(key)
        path.write_bytes(key.encode())

    storage.download.side_effect = _download
    checker = _make_checker_with_storage(tmp_path, storage)
    out: list[str] = []
    checker._out = out.append
    return checker, out, downloaded


def _matched_keys(checker: PreflightChecker) -> list[str]:
    return [path.read_bytes().decode() for _, path in checker.result.matches]


class TestFindVendorInBlob:
    def test_zoho_id_with_exact_file_in_inbox_is_matched(self, tmp_path):
        checker, out, downloaded = _blob_checker(tmp_path)
        zoho = MagicMock()
        zoho.extract_invoice_ids.return_value = [
            {"id": "INV-1", "url": "https://mail.example/1"},
        ]
        blobs = [
            _blob_at("faktury/inbox/zv_other.pdf", 20),
            _blob_at("faktury/inbox/zv_INV-1.pdf", 1),
        ]

        found = checker._find_vendor_in_blob(
            _zoho_vendor(), blobs, zoho, "2025/06/01", "2025/07/15"
        )

        assert found is True
        assert _matched_keys(checker) == ["faktury/inbox/zv_INV-1.pdf"]
        assert checker.result.matches[0][0].name == "ZohoVendor"
        assert downloaded == ["faktury/inbox/zv_INV-1.pdf"]
        assert any("found zv_INV-1.pdf (from blob)" in line for line in out)
        zoho.extract_invoice_ids.assert_called_once_with(
            search_term="billing@zohovendor.example",
            date_from="2025/06/01",
            date_to="2025/07/15",
            invoice_id_re=r"INV-\d+",
        )

    def test_exact_file_missing_falls_back_to_newest_glob_match(self, tmp_path):
        checker, out, downloaded = _blob_checker(tmp_path)
        zoho = MagicMock()
        zoho.extract_invoice_ids.return_value = [{"id": "INV-9", "url": None}]
        blobs = [
            _blob_at("faktury/inbox/zv_old.pdf", 2),
            _blob_at("faktury/inbox/zv_newest.pdf", 28),
            _blob_at("faktury/inbox/zv_middle.pdf", 14),
            _blob_at("faktury/inbox/unrelated.pdf", 30),
        ]

        found = checker._find_vendor_in_blob(
            _zoho_vendor(), blobs, zoho, "2025/06/01", "2025/07/15"
        )

        assert found is True
        assert _matched_keys(checker) == ["faktury/inbox/zv_newest.pdf"]
        assert downloaded == ["faktury/inbox/zv_newest.pdf"]
        assert any("found zv_newest.pdf (glob fallback)" in line for line in out)

    def test_exact_file_missing_and_no_glob_match_lists_email_urls(self, tmp_path):
        checker, out, downloaded = _blob_checker(tmp_path)
        zoho = MagicMock()
        zoho.extract_invoice_ids.return_value = [
            {"id": "INV-1", "url": "https://mail.example/1"},
            {"id": "INV-2", "url": "https://mail.example/2"},
        ]
        blobs = [_blob_at("faktury/inbox/unrelated.pdf", 10)]
        vendor = _zoho_vendor(fallback_url="https://vendor.example/billing")

        found = checker._find_vendor_in_blob(vendor, blobs, zoho, "2025/06/01", "2025/07/15")

        assert found is False
        assert checker.result.matches == []
        assert downloaded == []
        joined = "\n".join(out)
        assert "ZohoVendor: expected zv_INV-1.pdf, zv_INV-2.pdf — not in blob inbox/" in joined
        assert "🔗 https://mail.example/1" in joined
        assert "🔗 https://mail.example/2" in joined
        # E-mail URLs take precedence over the vendor's fallback URL.
        assert "https://vendor.example/billing" not in joined

    def test_exact_file_missing_no_urls_shows_fallback_url(self, tmp_path):
        checker, out, downloaded = _blob_checker(tmp_path)
        zoho = MagicMock()
        zoho.extract_invoice_ids.return_value = [{"id": "INV-3"}]
        vendor = _zoho_vendor(fallback_url="https://vendor.example/billing")

        found = checker._find_vendor_in_blob(vendor, [], zoho, "2025/06/01", "2025/07/15")

        assert found is False
        assert checker.result.matches == []
        assert downloaded == []
        joined = "\n".join(out)
        assert "ZohoVendor: expected zv_INV-3.pdf — not in blob inbox/" in joined
        assert "🔗 Download from: https://vendor.example/billing" in joined
        assert "https://mail.example" not in joined

    def test_zoho_failure_falls_through_to_glob_only_path(self, tmp_path, caplog):
        checker, out, _ = _blob_checker(tmp_path)
        zoho = MagicMock()
        zoho.extract_invoice_ids.side_effect = RuntimeError("zoho exploded")
        blobs = [
            _blob_at("faktury/inbox/zv_a.pdf", 3),
            _blob_at("faktury/inbox/zv_b.pdf", 25),
        ]

        with caplog.at_level("WARNING", logger="zdrovena.month_closing.preflight"):
            found = checker._find_vendor_in_blob(
                _zoho_vendor(), blobs, zoho, "2025/06/01", "2025/07/15"
            )

        assert found is True
        assert _matched_keys(checker) == ["faktury/inbox/zv_b.pdf"]
        # Glob-only path reports "(from blob)", not the post-Zoho "(glob fallback)".
        assert any("found zv_b.pdf (from blob)" in line for line in out)
        assert not any("expected" in line for line in out)
        assert any(
            r.levelname == "WARNING"
            and "Zoho invoice ID extraction for ZohoVendor failed" in r.getMessage()
            and "zoho exploded" in r.getMessage()
            for r in caplog.records
        )

    def test_zoho_failure_with_no_glob_match_reports_missing(self, tmp_path, caplog):
        checker, out, _ = _blob_checker(tmp_path)
        zoho = MagicMock()
        zoho.extract_invoice_ids.side_effect = RuntimeError("zoho exploded")
        vendor = _zoho_vendor(fallback_url="https://vendor.example/billing")

        with caplog.at_level("WARNING", logger="zdrovena.month_closing.preflight"):
            found = checker._find_vendor_in_blob(vendor, [], zoho, "2025/06/01", "2025/07/15")

        assert found is False
        assert checker.result.matches == []
        joined = "\n".join(out)
        assert "ZohoVendor: no matching PDF in blob inbox/" in joined
        assert "🔗 Download from: https://vendor.example/billing" in joined
        assert any(
            r.levelname == "WARNING" and "zoho exploded" in r.getMessage() for r in caplog.records
        )

    def test_failed_download_of_exact_file_is_not_counted_as_found(self, tmp_path):
        exact = "faktury/inbox/zv_INV-1.pdf"
        checker, out, downloaded = _blob_checker(tmp_path, failing_keys=frozenset({exact}))
        zoho = MagicMock()
        zoho.extract_invoice_ids.return_value = [{"id": "INV-1", "url": "https://mail.example/1"}]
        blobs = [_blob_at(exact, 10)]

        found = checker._find_vendor_in_blob(
            _zoho_vendor(), blobs, zoho, "2025/06/01", "2025/07/15"
        )

        assert found is False
        assert checker.result.matches == []
        assert downloaded == []
        joined = "\n".join(out)
        assert "✅" not in joined
        assert "ZohoVendor: expected zv_INV-1.pdf — not in blob inbox/" in joined
        assert "🔗 https://mail.example/1" in joined

    def test_failed_download_of_exact_file_retried_by_glob_still_not_found(self, tmp_path):
        exact = "faktury/inbox/zv_INV-1.pdf"
        checker, out, downloaded = _blob_checker(tmp_path, failing_keys=frozenset({exact}))
        zoho = MagicMock()
        zoho.extract_invoice_ids.return_value = [{"id": "INV-1"}]
        # The exact file is also the newest glob match, so the fallback retries
        # it and fails again — the older readable blob is not consulted.
        blobs = [_blob_at(exact, 20), _blob_at("faktury/inbox/zv_older.pdf", 5)]

        found = checker._find_vendor_in_blob(
            _zoho_vendor(), blobs, zoho, "2025/06/01", "2025/07/15"
        )

        assert found is False
        assert checker.result.matches == []
        assert downloaded == []
        assert checker._storage.download.call_count == 2
        assert "✅" not in "\n".join(out)

    def test_failed_download_of_newest_glob_match_is_not_counted(self, tmp_path):
        newest = "faktury/inbox/zv_new.pdf"
        checker, out, downloaded = _blob_checker(tmp_path, failing_keys=frozenset({newest}))
        blobs = [_blob_at("faktury/inbox/zv_old.pdf", 1), _blob_at(newest, 29)]

        found = checker._find_vendor_in_blob(
            _zoho_vendor(fallback_url="https://vendor.example/billing"),
            blobs,
            None,
            "2025/06/01",
            "2025/07/15",
        )

        assert found is False
        assert checker.result.matches == []
        assert downloaded == []
        joined = "\n".join(out)
        assert "✅" not in joined
        assert "ZohoVendor: no matching PDF in blob inbox/" in joined
        assert "🔗 Download from: https://vendor.example/billing" in joined
