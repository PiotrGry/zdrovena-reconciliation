#!/usr/bin/env python3
"""Ensure the Azure Blob container used by the API exists (idempotent).

Azure Blob Storage never auto-creates containers, and neither Azurite nor the
FastAPI app do it on startup. Run this after the storage backend is up so the
first real API request doesn't hit a raw ContainerNotFound 500 (see
zdrovena/common/storage.py, zdrovena/api/routers/files.py).

Usage:
    python3 scripts/ensure-storage-container.py

Env:
    AZURE_STORAGE_CONNECTION_STRING / AZURE_STORAGE_ACCOUNT_URL  -> required
    AZURE_STORAGE_CONTAINER  (default: zdrovena-files)
    If neither is set, storage falls back to LocalStorageService (no
    container concept) and this script is a no-op.
"""

from __future__ import annotations

import os
import sys

_CONTAINER = os.environ.get("AZURE_STORAGE_CONTAINER", "zdrovena-files")


def main() -> int:
    account_url = os.environ.get("AZURE_STORAGE_ACCOUNT_URL")
    conn = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
    if not account_url and not conn:
        print("No Azure Blob config found — using LocalStorageService, nothing to do.")
        return 0

    from azure.core.exceptions import ResourceExistsError
    from azure.storage.blob import BlobServiceClient

    if account_url:
        from azure.identity import DefaultAzureCredential

        client = BlobServiceClient(account_url, credential=DefaultAzureCredential())
    else:
        assert conn is not None  # guarded by the account_url/conn check above
        client = BlobServiceClient.from_connection_string(conn)

    try:
        client.create_container(_CONTAINER)
        print(f"Created container {_CONTAINER!r}.")
    except ResourceExistsError:
        print(f"Container {_CONTAINER!r} already exists.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
