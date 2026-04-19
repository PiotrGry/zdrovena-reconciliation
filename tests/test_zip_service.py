"""Tests for zdrovena.month_closing.zip_service."""

from __future__ import annotations

import zipfile

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
