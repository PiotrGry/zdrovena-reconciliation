#!/usr/bin/env python3
"""Seed local Azurite storage with fake invoice files for month-closing tests.

Creates realistic test files in faktury/inbox/ so that preflight checks pass
and the full close-month pipeline can be exercised locally end-to-end.

Usage:
    python3 scripts/seed-local-storage.py             # seeds previous month
    python3 scripts/seed-local-storage.py --year 2026 --month 5
    python3 scripts/seed-local-storage.py --clear     # remove all inbox files first

Requires Azurite running: docker compose up azurite
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

AZURITE_CONNECTION_STRING = (
    "DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;"
    "AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tiqFhM8YKHkNATVD6wd+0=;"
    "BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1;"
)
CONTAINER = "zdrovena-files"
INBOX = "faktury/inbox"

FAKE_PDF = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n"
FAKE_XML = b'<?xml version="1.0" encoding="UTF-8"?>\n<root><test>seed data</test></root>\n'


def get_client():
    try:
        from azure.storage.blob import BlobServiceClient
    except ImportError:
        print("ERROR: azure-storage-blob not installed. Run: pip install azure-storage-blob")
        sys.exit(1)

    if account_url := os.environ.get("AZURE_STORAGE_ACCOUNT_URL"):
        # Real Azure — use DefaultAzureCredential (OIDC in CI, az login locally)
        from azure.identity import DefaultAzureCredential
        print(f"Using Azure storage: {account_url}")
        return BlobServiceClient(account_url=account_url, credential=DefaultAzureCredential())

    conn = os.environ.get("AZURE_STORAGE_CONNECTION_STRING", AZURITE_CONNECTION_STRING)
    print(f"Using connection string: {conn[:60]}...")
    return BlobServiceClient.from_connection_string(conn)


def ensure_container(client) -> None:
    container = client.get_container_client(CONTAINER)
    try:
        container.create_container()
        print(f"✓ Created container: {CONTAINER}")
    except Exception:
        pass  # already exists


def upload(client, key: str, data: bytes) -> None:
    blob = client.get_blob_client(container=CONTAINER, blob=key)
    blob.upload_blob(data, overwrite=True)
    print(f"  ↑ {key}")


def clear_inbox(client) -> None:
    container = client.get_container_client(CONTAINER)
    blobs = list(container.list_blobs(name_starts_with=f"{INBOX}/"))
    if not blobs:
        print("  (inbox already empty)")
        return
    for blob in blobs:
        container.delete_blob(blob.name)
        print(f"  ✗ deleted {blob.name}")


def seed(year: int, month: int, client) -> None:
    """Upload all fake files needed for closing month `year/month`."""
    # Bank statement filename convention: Wyciag_na_zadanie_{next_month_first_day}NNN.pdf
    if month == 12:
        next_year, next_month = year + 1, 1
    else:
        next_year, next_month = year, month + 1
    bank_date = f"{next_year}{next_month:02d}01"

    files: list[tuple[str, bytes]] = [
        # PKO BP bank statement (Wyciag) — next month's 1st day in filename
        (f"{INBOX}/Wyciag_na_zadanie_{bank_date}001.pdf", FAKE_PDF),

        # Canva invoice — pattern: invoice-XXXXX-YYYYMMDD.pdf
        (f"{INBOX}/invoice-12345-{year}{month:02d}15.pdf", FAKE_PDF),

        # Google Ads invoice — pattern: 10-digit number
        (f"{INBOX}/3849995102.pdf", FAKE_PDF),

        # Fakturownia JPK_FA report
        (f"{INBOX}/zdrovena-{year}-{month:02d}-01-jpk_fa.xml", FAKE_XML),

        # Fakturownia JPK_V7M report
        (f"{INBOX}/zdrovena-{year}-{month:02d}-01-jpkv7m.xml", FAKE_XML),

        # VAT sales register PDF
        (f"{INBOX}/zdrovena-{year}-{month:02d}-01_wykaz_sprzedazy.pdf", FAKE_PDF),
    ]

    print(f"\nSeeding inbox for {year}/{month:02d}:")
    for key, data in files:
        upload(client, key, data)

    print(f"\n✅ {len(files)} files uploaded to {CONTAINER}/{INBOX}/")
    print(f"\nTest close month with:")
    print(f"  curl -s -X POST http://localhost:8000/api/close \\")
    print(f"    -H 'Content-Type: application/json' \\")
    print(f"    -d '{{\"year\": {year}, \"month\": {month}, \"dry_run\": true}}' | python3 -m json.tool")


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed local Azurite storage with test invoice files")
    now = datetime.now(tz=timezone.utc)
    prev_month = now.month - 1 or 12
    prev_year = now.year if now.month > 1 else now.year - 1
    parser.add_argument("--year", type=int, default=prev_year)
    parser.add_argument("--month", type=int, default=prev_month)
    parser.add_argument("--clear", action="store_true", help="Clear inbox before seeding")
    args = parser.parse_args()

    client = get_client()
    ensure_container(client)

    if args.clear:
        print("\nClearing inbox...")
        clear_inbox(client)

    seed(args.year, args.month, client)


if __name__ == "__main__":
    main()
