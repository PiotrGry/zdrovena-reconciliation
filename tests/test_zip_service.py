"""Tests for zdrovena.month_closing.zip_service."""

from __future__ import annotations

import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from zdrovena.month_closing.zip_service import create_month_archive


class TestCreateMonthArchive:
    def test_creates_zip(self, tmp_path):
        (tmp_path / "invoice.pdf").write_text("pdf content")
        (tmp_path / "report.xml").write_text("<xml/>")

        result = create_month_archive(tmp_path, "styczen", 2025)

        assert result.name == "styczen_2025_HUMIO.zip"
        assert result.exists()
        assert result.stat().st_size > 0

    def test_zip_contains_files(self, tmp_path):
        (tmp_path / "a.pdf").write_text("aaa")
        (tmp_path / "b.xml").write_text("bbb")

        result = create_month_archive(tmp_path, "luty", 2025)

        with zipfile.ZipFile(result) as zf:
            names = zf.namelist()
            assert "a.pdf" in names
            assert "b.xml" in names

    def test_excludes_state_files(self, tmp_path):
        (tmp_path / "invoice.pdf").write_text("data")
        (tmp_path / ".state.json").write_text("{}")
        (tmp_path / ".file_hashes.json").write_text("{}")
        (tmp_path / ".DS_Store").write_bytes(b"\x00")

        result = create_month_archive(tmp_path, "marzec", 2025)

        with zipfile.ZipFile(result) as zf:
            names = zf.namelist()
            assert "invoice.pdf" in names
            assert ".state.json" not in names
            assert ".file_hashes.json" not in names
            assert ".DS_Store" not in names

    def test_excludes_itself(self, tmp_path):
        (tmp_path / "invoice.pdf").write_text("data")

        result = create_month_archive(tmp_path, "kwiecien", 2025)

        with zipfile.ZipFile(result) as zf:
            names = zf.namelist()
            assert "kwiecien_2025_HUMIO.zip" not in names

    def test_includes_subdirectories(self, tmp_path):
        sub = tmp_path / "sales"
        sub.mkdir()
        (sub / "fv_01.pdf").write_text("invoice 1")

        result = create_month_archive(tmp_path, "maj", 2025)

        with zipfile.ZipFile(result) as zf:
            names = zf.namelist()
            # Relative path preserved
            assert any("fv_01.pdf" in n for n in names)

    def test_overwrites_existing_zip(self, tmp_path):
        (tmp_path / "a.txt").write_text("first")

        zip1 = create_month_archive(tmp_path, "czerwiec", 2025)
        size1 = zip1.stat().st_size

        (tmp_path / "b.txt").write_text("second file with more content")
        zip2 = create_month_archive(tmp_path, "czerwiec", 2025)
        size2 = zip2.stat().st_size

        assert zip1 == zip2  # same path
        assert size2 > size1  # larger because more content

    def test_empty_directory(self, tmp_path):
        result = create_month_archive(tmp_path, "lipiec", 2025)
        assert result.exists()
        with zipfile.ZipFile(result) as zf:
            assert len(zf.namelist()) == 0


# ── create_month_archive_from_blob ────────────────────────────────────────────


class TestCreateMonthArchiveFromBlob:
    def _make_blob(self, key, content=b"data"):
        blob = MagicMock()
        blob.key = key
        return blob, content

    def _make_storage(self, blobs_and_contents):
        storage = MagicMock()
        blob_objects = []
        content_map = {}
        for blob, content in blobs_and_contents:
            blob_objects.append(blob)
            content_map[blob.key] = content

        storage.list_files.return_value = blob_objects
        storage.stream.side_effect = lambda key: iter([content_map[key]])
        return storage

    def test_creates_zip_from_blobs(self):
        from zdrovena.month_closing.zip_service import create_month_archive_from_blob

        blob1, data1 = self._make_blob("faktury/2026/kwiecien/FV001.pdf", b"pdf content")
        blob2, data2 = self._make_blob("faktury/2026/kwiecien/FV002.pdf", b"pdf2 content")
        storage = self._make_storage([(blob1, data1), (blob2, data2)])

        key, count, _ = create_month_archive_from_blob(
            storage, "faktury/2026/kwiecien", "kwiecien", 2026
        )

        assert key == "faktury/2026/kwiecien/kwiecien_2026_HUMIO.zip"
        assert count == 2
        storage.upload_stream.assert_called_once()

    def test_excludes_zip_files(self):
        from zdrovena.month_closing.zip_service import create_month_archive_from_blob

        blob_pdf, _ = self._make_blob("faktury/2026/kwiecien/FV001.pdf", b"pdf")
        blob_zip, _ = self._make_blob("faktury/2026/kwiecien/old.zip", b"zip")
        storage = self._make_storage([(blob_pdf, b"pdf"), (blob_zip, b"zip")])

        _, count, _files = create_month_archive_from_blob(
            storage, "faktury/2026/kwiecien", "kwiecien", 2026
        )

        assert count == 1

    def test_excludes_state_files(self):
        from zdrovena.month_closing.zip_service import create_month_archive_from_blob

        blob_pdf, _ = self._make_blob("faktury/2026/kwiecien/FV001.pdf", b"pdf")
        blob_state, _ = self._make_blob("faktury/2026/kwiecien/.state.json", b"{}")
        storage = self._make_storage([(blob_pdf, b"pdf"), (blob_state, b"{}")])

        _, count, _files = create_month_archive_from_blob(
            storage, "faktury/2026/kwiecien", "kwiecien", 2026
        )

        assert count == 1

    def test_empty_prefix_returns_zero(self):
        from zdrovena.month_closing.zip_service import create_month_archive_from_blob

        storage = MagicMock()
        storage.list_files.return_value = []
        _, count, _files = create_month_archive_from_blob(
            storage, "faktury/2026/kwiecien", "kwiecien", 2026
        )
        assert count == 0


# ── Month-scoped inbox in the monthly archive (pins hotfix #480) ──────────────

_SCOPED_INBOX = "faktury/inbox/2026-08"
_SCOPED_INBOX_FILES = {
    "Wyciag_8_12345678_202608311.pdf": b"%PDF statement",
    "historia_sierpien.pdf": b"%PDF unnamed statement",
    "raporty/rejestr_sprzedazy.xml": b"<xml/>",
    "raporty/2026/notatka.txt": b"note",
    "paczka_od_dostawcy.zip": b"PK uploaded archive",
}


class TestScopedInboxInMonthlyArchive:
    """Every file the operator uploaded for the period reaches the accountant.

    Driven through the real ZIP step over local storage, so the copy of the
    month-scoped inbox into the month tree and the archive build run together.
    """

    INBOX = _SCOPED_INBOX
    INBOX_FILES = _SCOPED_INBOX_FILES

    def _run_zip_step(self, tmp_path: Path) -> zipfile.ZipFile:
        from zdrovena.common.storage import LocalStorageService
        from zdrovena.month_closing.orchestrator import MonthCloseOrchestrator

        storage = LocalStorageService(root=tmp_path / "storage")
        for relative, content in self.INBOX_FILES.items():
            target = storage.root / self.INBOX / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        collected = storage.root / "faktury/2026/sierpien/koszty/FV001.pdf"
        collected.parent.mkdir(parents=True, exist_ok=True)
        collected.write_bytes(b"%PDF cost invoice")

        with patch("zdrovena.month_closing.orchestrator.get_storage_service", return_value=storage):
            orch = MonthCloseOrchestrator(
                year=2026, month=8, dry_run=False, inbox_prefix=self.INBOX
            )
        orch.out = MagicMock()
        orch.month_dir = tmp_path / "local" / "2026" / "sierpien"
        orch.month_dir.mkdir(parents=True)

        orch._step_6_zip_archive()

        assert str(orch.report.zip_path) == "faktury/2026/sierpien/sierpien_2026_HUMIO.zip"
        return zipfile.ZipFile(storage.root / orch.report.zip_path)

    def test_every_inbox_file_is_archived_with_its_inbox_path(self, tmp_path):
        with self._run_zip_step(tmp_path) as zf:
            names = set(zf.namelist())
            for relative, content in self.INBOX_FILES.items():
                assert f"inbox/{relative}" in names
                assert zf.read(f"inbox/{relative}") == content

    def test_collected_documents_stay_next_to_the_inbox(self, tmp_path):
        with self._run_zip_step(tmp_path) as zf:
            assert "koszty/FV001.pdf" in zf.namelist()

    def test_nested_zip_outside_the_inbox_is_still_skipped(self):
        from zdrovena.month_closing.zip_service import create_month_archive_from_blob

        storage = MagicMock()
        blobs = []
        for key in (
            "faktury/2026/sierpien/old_package.zip",
            "faktury/2026/sierpien/inbox/paczka.zip",
        ):
            blob = MagicMock()
            blob.key = key
            blobs.append(blob)
        storage.list_files.return_value = blobs
        storage.stream.side_effect = lambda key: iter([b"zip"])

        _, _, files = create_month_archive_from_blob(
            storage, "faktury/2026/sierpien", "sierpien", 2026
        )

        assert files == ["inbox/paczka.zip"]
