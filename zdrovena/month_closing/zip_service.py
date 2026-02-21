"""
zdrovena.month_closing.zip_service – ZIP Archive Service
==========================================================
Creates a ZIP archive of all collected accounting documents for a given month.
"""

from __future__ import annotations

import logging
import zipfile
from pathlib import Path

logger = logging.getLogger("zdrovena.month_closing.zip")


def create_month_archive(month_dir: Path, month_name_pl: str, year: int) -> Path:
    zip_name = f"{month_name_pl}_{year}_HUMIO.zip"
    zip_path = month_dir / zip_name

    if zip_path.exists():
        zip_path.unlink()

    excluded_names = {zip_name, ".file_hashes.json", ".state.json", ".DS_Store"}
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
