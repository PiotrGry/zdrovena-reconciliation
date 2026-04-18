"""
zdrovena.common.storage – Storage abstraction layer
=====================================================
Single entry-point for file storage operations.

Implementations:
  LocalStorageService  — dev/tests, files in ~/.zdrovena/storage/
  BlobStorageService   — production, Azure Blob Storage

Resolution (get_storage_service factory):
  AZURE_STORAGE_CONNECTION_STRING set → BlobStorageService
  otherwise                           → LocalStorageService

Usage::

    storage = get_storage_service()
    storage.upload(Path("invoice.pdf"), "2026/03/invoice.pdf")
    url = storage.get_download_url("2026/03/invoice.pdf", ttl_minutes=15)
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol, runtime_checkable

logger = logging.getLogger("zdrovena.common.storage")

try:
    from azure.storage.blob import (
        BlobSasPermissions,
        BlobServiceClient,
        generate_blob_sas,
    )
    _AZURE_STORAGE_AVAILABLE = True
except ImportError:
    BlobSasPermissions = None  # type: ignore[assignment,misc]
    BlobServiceClient = None   # type: ignore[assignment,misc]
    generate_blob_sas = None   # type: ignore[assignment]
    _AZURE_STORAGE_AVAILABLE = False

_DEFAULT_ROOT = Path.home() / ".zdrovena" / "storage"
_DEFAULT_CONTAINER = "month-closing"


@dataclass
class BlobFile:
    key: str
    size: int
    last_modified: datetime


# ── Protocol ──────────────────────────────────────────────────────────────────

@runtime_checkable
class StorageService(Protocol):
    def upload(self, local_path: Path, key: str) -> None: ...
    def download(self, key: str, local_path: Path) -> None: ...
    def list_files(self, prefix: str = "") -> list[BlobFile]: ...
    def get_download_url(self, key: str, ttl_minutes: int = 15) -> str: ...
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

    def download(self, key: str, local_path: Path) -> None:
        src = self.root / key
        if not src.exists():
            raise FileNotFoundError(f"Key not found in local storage: {key!r}")
        local_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, local_path)

    def list_files(self, prefix: str = "") -> list[BlobFile]:
        results: list[BlobFile] = []
        base = self.root / prefix if prefix else self.root
        if not base.exists():
            return results
        for path in sorted(base.rglob("*")):
            if path.is_file():
                stat = path.stat()
                key = path.relative_to(self.root).as_posix()
                results.append(BlobFile(
                    key=key,
                    size=stat.st_size,
                    last_modified=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                ))
        return results

    def get_download_url(self, key: str, ttl_minutes: int = 15) -> str:
        """Return file:// URL — TTL is ignored for local storage."""
        path = self.root / key
        return path.as_uri()

    def delete(self, key: str) -> None:
        path = self.root / key
        path.unlink(missing_ok=True)


# ── Azure Blob implementation ─────────────────────────────────────────────────

class BlobStorageService:
    """Azure Blob Storage backed service — requires [cloud] extras."""

    def __init__(self, connection_string: str, container: str = _DEFAULT_CONTAINER) -> None:
        if not _AZURE_STORAGE_AVAILABLE:
            raise RuntimeError(
                "Azure Blob dependencies not installed. "
                "Install with: pip install zdrovena-reconciliation[cloud]"
            )
        self._connection_string = connection_string
        self._container = container
        self._client = BlobServiceClient.from_connection_string(connection_string)
        logger.debug("BlobStorage: connected to container %r", container)

    def upload(self, local_path: Path, key: str) -> None:
        blob = self._client.get_blob_client(container=self._container, blob=key)
        with local_path.open("rb") as f:
            blob.upload_blob(f, overwrite=True)
        logger.debug("BlobStorage: uploaded %s → %s/%s", local_path, self._container, key)

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
                last_modified=b.last_modified or datetime.now(tz=timezone.utc),
            )
            for b in blobs
        ]

    def get_download_url(self, key: str, ttl_minutes: int = 15) -> str:
        expiry = datetime.now(tz=timezone.utc) + timedelta(minutes=ttl_minutes)
        account_name = self._client.account_name
        account_key = self._client.credential.account_key

        sas_token = generate_blob_sas(
            account_name=account_name,
            container_name=self._container,
            blob_name=key,
            account_key=account_key,
            permission=BlobSasPermissions(read=True),
            expiry=expiry,
        )
        return f"https://{account_name}.blob.core.windows.net/{self._container}/{key}?{sas_token}"

    def delete(self, key: str) -> None:
        blob = self._client.get_blob_client(container=self._container, blob=key)
        blob.delete_blob(delete_snapshots="include")


# ── Factory ───────────────────────────────────────────────────────────────────

def get_storage_service(
    root: Path | None = None,
    container: str | None = None,
) -> StorageService:
    """Return the appropriate StorageService based on environment.

    Parameters
    ----------
    root:
        Override root dir for LocalStorageService (useful in tests).
    container:
        Override Azure container name (default: ``AZURE_STORAGE_CONTAINER``
        env var or ``"month-closing"``).
    """
    conn = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
    if conn:
        resolved_container = (
            container
            or os.environ.get("AZURE_STORAGE_CONTAINER")
            or _DEFAULT_CONTAINER
        )
        return BlobStorageService(conn, resolved_container)
    return LocalStorageService(root=root)
