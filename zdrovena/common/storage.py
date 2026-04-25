"""
zdrovena.common.storage – Storage abstraction layer
=====================================================
Single entry-point for file storage operations.

Implementations:
  LocalStorageService  — dev/tests, files in ~/.zdrovena/storage/
  BlobStorageService   — production, Azure Blob Storage

Resolution (get_storage_service factory):
  AZURE_STORAGE_ACCOUNT_URL set        → BlobStorageService via DefaultAzureCredential (managed identity)
  AZURE_STORAGE_CONNECTION_STRING set  → BlobStorageService via connection string (Azurite emulator)
  otherwise                            → LocalStorageService

Download model (RBAC, no SAS):
  • Blob container: private, no public access
  • Required role on container: ``Storage Blob Data Contributor`` assigned to the app's managed identity
  • Clients call  GET /files/{key}  on FastAPI — never a direct blob URL
  • FastAPI calls  storage.stream(key)  and returns StreamingResponse authenticated by DefaultAzureCredential
  • Clients upload via  PUT /files/{key}  — requires zdrovena-accountant or zdrovena-admin role

Usage::

    storage = get_storage_service()
    storage.upload(Path("invoice.pdf"), "2026/03/invoice.pdf")
    for chunk in storage.stream("2026/03/invoice.pdf"):  # RBAC-checked by Azure
        ...
"""

from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, Protocol, runtime_checkable

logger = logging.getLogger("zdrovena.common.storage")

try:
    from azure.storage.blob import BlobServiceClient, ContentSettings

    _AZURE_STORAGE_AVAILABLE = True
except ImportError:
    BlobServiceClient = None  # type: ignore[assignment,misc]
    ContentSettings = None  # type: ignore[assignment,misc]
    _AZURE_STORAGE_AVAILABLE = False

try:
    from azure.identity import DefaultAzureCredential

    _AZURE_IDENTITY_AVAILABLE = True
except ImportError:
    DefaultAzureCredential = None  # type: ignore[assignment,misc]
    _AZURE_IDENTITY_AVAILABLE = False

_DEFAULT_ROOT = Path.home() / ".zdrovena" / "storage"
_DEFAULT_CONTAINER = "zdrovena-files"


@dataclass
class BlobFile:
    key: str
    size: int
    last_modified: datetime


# ── Protocol ──────────────────────────────────────────────────────────────────


@runtime_checkable
class StorageService(Protocol):
    def upload(self, local_path: Path, key: str) -> None: ...
    def upload_stream(
        self, data: BinaryIO, key: str, content_type: str = "application/octet-stream"
    ) -> None: ...
    def download(self, key: str, local_path: Path) -> None: ...
    def stream(self, key: str, chunk_size: int = 4 * 1024 * 1024) -> Iterator[bytes]: ...
    def list_files(self, prefix: str = "") -> list[BlobFile]: ...
    def exists(self, key: str) -> bool: ...
    def delete(self, key: str) -> None: ...


# ── Local implementation ──────────────────────────────────────────────────────


class LocalStorageService:
    """File-system backed storage for local dev and tests."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or _DEFAULT_ROOT

    def upload(self, local_path: Path, key: str) -> None:
        dest = self.root / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local_path, dest)
        logger.debug("LocalStorage: uploaded %s → %s", local_path, dest)

    def upload_stream(
        self, data: BinaryIO, key: str, content_type: str = "application/octet-stream"
    ) -> None:
        dest = self.root / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as f:
            shutil.copyfileobj(data, f)
        logger.debug("LocalStorage: upload_stream → %s", dest)

    def download(self, key: str, local_path: Path) -> None:
        src = self.root / key
        if not src.exists():
            raise FileNotFoundError(f"Key not found in local storage: {key!r}")
        local_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, local_path)

    def stream(self, key: str, chunk_size: int = 4 * 1024 * 1024) -> Iterator[bytes]:
        """Yield file content in chunks. For local dev/tests only."""
        path = self.root / key
        if not path.exists():
            raise FileNotFoundError(f"Key not found in local storage: {key!r}")
        with path.open("rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                yield chunk

    def list_files(self, prefix: str = "") -> list[BlobFile]:
        results: list[BlobFile] = []
        base = self.root / prefix if prefix else self.root
        if not base.exists():
            return results
        for path in sorted(base.rglob("*")):
            if path.is_file():
                stat = path.stat()
                key = path.relative_to(self.root).as_posix()
                results.append(
                    BlobFile(
                        key=key,
                        size=stat.st_size,
                        last_modified=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
                    )
                )
        return results

    def delete(self, key: str) -> None:
        path = self.root / key
        path.unlink(missing_ok=True)

    def exists(self, key: str) -> bool:
        return (self.root / key).is_file()


# ── Azure Blob implementation ─────────────────────────────────────────────────


class BlobStorageService:
    """Azure Blob Storage backed service — requires [cloud] extras.

    Authentication:
    - ``account_url`` → DefaultAzureCredential (managed identity / ``az login``)
    - ``connection_string`` → raw connection string (Azurite emulator only)

    Download model (RBAC, no SAS):
    - Container must be private (no public access)
    - Required role on container: ``Storage Blob Data Reader`` on the app’s managed identity
    - FastAPI calls ``stream(key)`` and returns StreamingResponse — clients never get a blob URL
    """

    def __init__(
        self,
        *,
        account_url: str | None = None,
        connection_string: str | None = None,
        container: str = _DEFAULT_CONTAINER,
    ) -> None:
        if not _AZURE_STORAGE_AVAILABLE:
            raise RuntimeError(
                "Azure Blob dependencies not installed. "
                "Install with: pip install zdrovena-reconciliation[cloud]"
            )
        if account_url and connection_string:
            raise ValueError("Provide either account_url or connection_string, not both.")
        if not account_url and not connection_string:
            raise ValueError(
                "Provide account_url (managed identity) or connection_string (emulator)."
            )
        self._container = container
        if account_url:
            if not _AZURE_IDENTITY_AVAILABLE:
                raise RuntimeError(
                    "azure-identity not installed. "
                    "Install with: pip install zdrovena-reconciliation[cloud]"
                )
            assert BlobServiceClient is not None  # guarded by _AZURE_STORAGE_AVAILABLE check above
            assert DefaultAzureCredential is not None
            self._client = BlobServiceClient(account_url, credential=DefaultAzureCredential())
            logger.debug("BlobStorage: connected via DefaultAzureCredential to %r", account_url)
        else:
            assert BlobServiceClient is not None  # guarded by _AZURE_STORAGE_AVAILABLE check above
            assert connection_string is not None  # guarded by ValueError check above
            self._client = BlobServiceClient.from_connection_string(connection_string)
            logger.debug("BlobStorage: connected via connection string, container %r", container)

    def upload(self, local_path: Path, key: str) -> None:
        blob = self._client.get_blob_client(container=self._container, blob=key)
        with local_path.open("rb") as f:
            blob.upload_blob(f, overwrite=True)
        logger.debug("BlobStorage: uploaded %s → %s/%s", local_path, self._container, key)

    def upload_stream(
        self, data: BinaryIO, key: str, content_type: str = "application/octet-stream"
    ) -> None:
        blob = self._client.get_blob_client(container=self._container, blob=key)
        blob.upload_blob(
            data,
            overwrite=True,
            content_settings=ContentSettings(content_type=content_type),  # type: ignore[misc]
        )
        logger.debug("BlobStorage: upload_stream → %s/%s", self._container, key)

    def download(self, key: str, local_path: Path) -> None:
        blob = self._client.get_blob_client(container=self._container, blob=key)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        with local_path.open("wb") as f:
            blob.download_blob().readinto(f)

    def list_files(self, prefix: str = "") -> list[BlobFile]:
        container = self._client.get_container_client(self._container)
        blobs = container.list_blobs(name_starts_with=prefix or None)
        return [
            BlobFile(
                key=b.name,
                size=b.size or 0,
                last_modified=b.last_modified or datetime.now(tz=UTC),
            )
            for b in blobs
        ]

    def stream(self, key: str, chunk_size: int = 4 * 1024 * 1024) -> Iterator[bytes]:
        """Yield blob content in chunks, authenticated via RBAC.

        Requires ``Storage Blob Data Reader`` role on the managed identity.
        Use in FastAPI: ``return StreamingResponse(storage.stream(key), media_type=...)``
        """
        blob = self._client.get_blob_client(container=self._container, blob=key)
        downloader = blob.download_blob()
        yield from downloader.chunks()

    def delete(self, key: str) -> None:
        blob = self._client.get_blob_client(container=self._container, blob=key)
        blob.delete_blob(delete_snapshots="include")

    def exists(self, key: str) -> bool:
        blob = self._client.get_blob_client(container=self._container, blob=key)
        return blob.exists()


# ── Factory ───────────────────────────────────────────────────────────────────


def get_storage_service(
    root: Path | None = None,
    container: str | None = None,
) -> StorageService:
    """Return the appropriate StorageService based on environment.

    Priority:
    1. ``AZURE_STORAGE_ACCOUNT_URL``       → BlobStorageService via DefaultAzureCredential
    2. ``AZURE_STORAGE_CONNECTION_STRING`` → BlobStorageService via connection string (Azurite)
    3. otherwise                           → LocalStorageService

    Parameters
    ----------
    root:
        Override root dir for LocalStorageService (useful in tests).
    container:
        Override Azure container name (default: ``AZURE_STORAGE_CONTAINER``
        env var or ``"month-closing"``).
    """
    resolved_container = (
        container or os.environ.get("AZURE_STORAGE_CONTAINER") or _DEFAULT_CONTAINER
    )
    account_url = os.environ.get("AZURE_STORAGE_ACCOUNT_URL")
    if account_url:
        return BlobStorageService(account_url=account_url, container=resolved_container)
    conn = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
    if conn:
        return BlobStorageService(connection_string=conn, container=resolved_container)
    return LocalStorageService(root=root)
