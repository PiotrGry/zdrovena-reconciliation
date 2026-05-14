"""
zdrovena.month_closing.zip_service – ZIP Archive Service
==========================================================
Creates a ZIP archive of all collected accounting documents for a given month.
Supports both local filesystem (legacy) and blob storage (production).
"""

from __future__ import annotations

import logging
import zipfile
from io import BytesIO
from pathlib import Path

logger = logging.getLogger("zdrovena.month_closing.zip")

_EXCLUDED = {".file_hashes.json", ".state.json", ".DS_Store"}


def create_month_archive(month_dir: Path, month_name_pl: str, year: int) -> Path:
    """Create ZIP from local filesystem. Used for local dev fallback."""
    zip_name = f"{month_name_pl}_{year}_HUMIO.zip"
    zip_path = month_dir / zip_name

    if zip_path.exists():
        zip_path.unlink()

    excluded_names = _EXCLUDED | {zip_name}
    count = 0

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in sorted(month_dir.rglob("*")):
            if not file_path.is_file():
                continue
            if file_path.name in excluded_names:
                continue
            arcname = file_path.relative_to(month_dir)
            zf.write(file_path, arcname)
            count += 1

    size_mb = zip_path.stat().st_size / (1024 * 1024)
    logger.info("ZIP created: %s  (%d files, %.2f MB)", zip_path.name, count, size_mb)
    return zip_path


def create_month_archive_from_blob(
    storage, blob_prefix: str, month_name_pl: str, year: int
) -> tuple[str, int]:
    """Create ZIP from blob storage and upload it back. Returns (blob_key, file_count).

    Reads all files from `blob_prefix/` via storage.stream(), builds ZIP in memory,
    uploads to `blob_prefix/{zip_name}`. No local filesystem writes needed.
    """
    zip_name = f"{month_name_pl}_{year}_HUMIO.zip"
    zip_key = f"{blob_prefix}/{zip_name}"
    excluded_names = _EXCLUDED | {zip_name}

    blobs = storage.list_files(blob_prefix + "/")
    buf = BytesIO()
    count = 0

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for blob in sorted(blobs, key=lambda b: b.key):
            filename = blob.key[len(blob_prefix) :].lstrip("/")
            # Skip excluded files and nested .zip files
            if Path(filename).name in excluded_names:
                continue
            if filename.endswith(".zip"):
                continue
            content = b"".join(storage.stream(blob.key))
            zf.writestr(filename, content)
            count += 1

    buf.seek(0)
    storage.upload_stream(buf, zip_key, "application/zip")
    size_mb = len(buf.getvalue()) / (1024 * 1024)
    logger.info("ZIP created from blob: %s  (%d files, %.2f MB)", zip_name, count, size_mb)
    return zip_key, count
