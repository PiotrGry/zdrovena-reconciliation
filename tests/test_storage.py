"""Tests for zdrovena.common.storage — LocalStorageService + BlobStorageService."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from zdrovena.common.storage import (
    BlobFile,
    BlobStorageService,
    LocalStorageService,
    StorageService,
    get_storage_service,
)

# ── LocalStorageService ───────────────────────────────────────────────────────


class TestLocalStorageService:
    def test_upload_creates_file(self, tmp_path):
        root = tmp_path / "storage"
        svc = LocalStorageService(root=root)
        src = tmp_path / "invoice.pdf"
        src.write_bytes(b"PDF content")

        svc.upload(src, "2026/03/invoice.pdf")

        assert (root / "2026/03/invoice.pdf").read_bytes() == b"PDF content"

    def test_upload_creates_intermediate_dirs(self, tmp_path):
        root = tmp_path / "storage"
        svc = LocalStorageService(root=root)
        src = tmp_path / "file.xml"
        src.write_bytes(b"<xml/>")

        svc.upload(src, "deep/nested/path/file.xml")

        assert (root / "deep/nested/path/file.xml").exists()

    def test_download_copies_file(self, tmp_path):
        root = tmp_path / "storage"
        svc = LocalStorageService(root=root)
        (root / "2026/03").mkdir(parents=True)
        (root / "2026/03/report.pdf").write_bytes(b"report")

        dest = tmp_path / "out/report.pdf"
        svc.download("2026/03/report.pdf", dest)

        assert dest.read_bytes() == b"report"

    def test_download_missing_key_raises(self, tmp_path):
        svc = LocalStorageService(root=tmp_path / "storage")
        with pytest.raises(FileNotFoundError, match=r"missing\.pdf"):
            svc.download("missing.pdf", tmp_path / "out.pdf")

    def test_list_files_returns_blob_files(self, tmp_path):
        root = tmp_path / "storage"
        svc = LocalStorageService(root=root)
        (root / "2026/03").mkdir(parents=True)
        (root / "2026/03/a.pdf").write_bytes(b"aaa")
        (root / "2026/03/b.xml").write_bytes(b"bb")

        files = svc.list_files("2026/03")

        keys = {f.key for f in files}
        assert "2026/03/a.pdf" in keys
        assert "2026/03/b.xml" in keys
        assert all(isinstance(f, BlobFile) for f in files)
        assert all(isinstance(f.last_modified, datetime) for f in files)

    def test_list_files_empty_prefix(self, tmp_path):
        root = tmp_path / "storage"
        svc = LocalStorageService(root=root)
        (root / "x").mkdir(parents=True)
        (root / "x/file.txt").write_bytes(b"x")

        files = svc.list_files()
        assert len(files) == 1
        assert files[0].key == "x/file.txt"

    def test_list_files_nonexistent_prefix_returns_empty(self, tmp_path):
        svc = LocalStorageService(root=tmp_path / "storage")
        assert svc.list_files("nonexistent/") == []

    def test_stream_yields_chunks(self, tmp_path):
        root = tmp_path / "storage"
        svc = LocalStorageService(root=root)
        (root / "2026/03").mkdir(parents=True)
        (root / "2026/03/report.pdf").write_bytes(b"chunk1" + b"chunk2")

        data = b"".join(svc.stream("2026/03/report.pdf", chunk_size=6))

        assert data == b"chunk1chunk2"

    def test_stream_missing_key_raises(self, tmp_path):
        svc = LocalStorageService(root=tmp_path / "storage")
        with pytest.raises(FileNotFoundError):
            list(svc.stream("missing.pdf"))

    def test_delete_removes_file(self, tmp_path):
        root = tmp_path / "storage"
        svc = LocalStorageService(root=root)
        f = root / "del.pdf"
        f.parent.mkdir(parents=True)
        f.write_bytes(b"x")

        svc.delete("del.pdf")

        assert not f.exists()

    def test_delete_missing_key_is_noop(self, tmp_path):
        svc = LocalStorageService(root=tmp_path / "storage")
        svc.delete("nonexistent.pdf")  # must not raise

    def test_implements_storage_service_protocol(self, tmp_path):
        svc = LocalStorageService(root=tmp_path / "storage")
        assert isinstance(svc, StorageService)


# ── get_storage_service factory ───────────────────────────────────────────────


class TestGetStorageServiceFactory:
    def test_returns_local_without_env(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AZURE_STORAGE_CONNECTION_STRING", raising=False)
        monkeypatch.delenv("AZURE_STORAGE_ACCOUNT_URL", raising=False)
        svc = get_storage_service(root=tmp_path)
        assert isinstance(svc, LocalStorageService)

    def test_local_uses_provided_root(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AZURE_STORAGE_CONNECTION_STRING", raising=False)
        monkeypatch.delenv("AZURE_STORAGE_ACCOUNT_URL", raising=False)
        svc = get_storage_service(root=tmp_path)
        assert isinstance(svc, LocalStorageService)
        assert svc.root == tmp_path

    def test_returns_blob_with_account_url(self, monkeypatch):
        monkeypatch.setenv("AZURE_STORAGE_ACCOUNT_URL", "https://myaccount.blob.core.windows.net")
        monkeypatch.delenv("AZURE_STORAGE_CONNECTION_STRING", raising=False)
        with patch(
            "zdrovena.common.storage.BlobStorageService.__init__", return_value=None
        ) as mock_init:
            get_storage_service()
            _, kwargs = mock_init.call_args
            assert kwargs.get("account_url") == "https://myaccount.blob.core.windows.net"

    def test_returns_blob_with_connection_string(self, monkeypatch):
        monkeypatch.delenv("AZURE_STORAGE_ACCOUNT_URL", raising=False)
        monkeypatch.setenv(
            "AZURE_STORAGE_CONNECTION_STRING",
            "DefaultEndpointsProtocol=https;AccountName=test;AccountKey=dGVzdA==;EndpointSuffix=core.windows.net",
        )
        with patch(
            "zdrovena.common.storage.BlobStorageService.__init__", return_value=None
        ) as mock_init:
            get_storage_service()
            _, kwargs = mock_init.call_args
            assert kwargs.get("connection_string") is not None

    def test_account_url_takes_priority_over_connection_string(self, monkeypatch):
        monkeypatch.setenv("AZURE_STORAGE_ACCOUNT_URL", "https://myaccount.blob.core.windows.net")
        monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", "conn_str")
        with patch(
            "zdrovena.common.storage.BlobStorageService.__init__", return_value=None
        ) as mock_init:
            get_storage_service()
            _, kwargs = mock_init.call_args
            assert kwargs.get("account_url") is not None
            assert kwargs.get("connection_string") is None

    def test_blob_uses_env_container(self, monkeypatch):
        monkeypatch.delenv("AZURE_STORAGE_ACCOUNT_URL", raising=False)
        monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", "conn_str")
        monkeypatch.setenv("AZURE_STORAGE_CONTAINER", "my-container")
        with patch(
            "zdrovena.common.storage.BlobStorageService.__init__", return_value=None
        ) as mock_init:
            get_storage_service()
            _, kwargs = mock_init.call_args
            assert kwargs.get("container") == "my-container"


# ── BlobStorageService (mocked) ───────────────────────────────────────────────


class TestBlobStorageServiceMocked:
    """Test BlobStorageService with azure SDK fully mocked."""

    def _make_svc(self):
        with patch("zdrovena.common.storage.BlobStorageService.__init__", return_value=None):
            svc = BlobStorageService.__new__(BlobStorageService)
            svc._container = "month-closing"
            svc._connection_string = "fake"
            svc._client = MagicMock()
        return svc

    def test_upload_calls_upload_blob(self, tmp_path):
        svc = self._make_svc()
        src = tmp_path / "file.pdf"
        src.write_bytes(b"data")
        mock_blob = MagicMock()
        svc._client.get_blob_client.return_value = mock_blob

        svc.upload(src, "2026/03/file.pdf")

        svc._client.get_blob_client.assert_called_once_with(
            container="month-closing", blob="2026/03/file.pdf"
        )
        mock_blob.upload_blob.assert_called_once()

    def test_download_writes_to_file(self, tmp_path):
        svc = self._make_svc()
        mock_blob = MagicMock()
        mock_blob.download_blob.return_value.readinto = MagicMock()
        svc._client.get_blob_client.return_value = mock_blob
        dest = tmp_path / "out.pdf"

        svc.download("2026/03/file.pdf", dest)

        mock_blob.download_blob.return_value.readinto.assert_called_once()

    def test_list_files_returns_blob_files(self):
        svc = self._make_svc()
        mock_container = MagicMock()
        svc._client.get_container_client.return_value = mock_container
        mock_blob = MagicMock()
        mock_blob.name = "2026/03/file.pdf"
        mock_blob.size = 1024
        mock_blob.last_modified = datetime(2026, 3, 1, tzinfo=timezone.utc)
        mock_container.list_blobs.return_value = [mock_blob]

        files = svc.list_files("2026/03")

        assert len(files) == 1
        assert files[0].key == "2026/03/file.pdf"
        assert files[0].size == 1024

    def test_delete_calls_delete_blob(self):
        svc = self._make_svc()
        mock_blob = MagicMock()
        svc._client.get_blob_client.return_value = mock_blob

        svc.delete("2026/03/file.pdf")

        mock_blob.delete_blob.assert_called_once_with(delete_snapshots="include")

    def test_stream_yields_chunks_via_sdk(self):
        svc = self._make_svc()
        mock_blob = MagicMock()
        mock_blob.download_blob.return_value.chunks.return_value = iter([b"part1", b"part2"])
        svc._client.get_blob_client.return_value = mock_blob

        data = b"".join(svc.stream("2026/03/file.pdf"))

        assert data == b"part1part2"
        mock_blob.download_blob.return_value.chunks.assert_called_once()

    def test_implements_storage_service_protocol(self):
        svc = self._make_svc()
        assert isinstance(svc, StorageService)
