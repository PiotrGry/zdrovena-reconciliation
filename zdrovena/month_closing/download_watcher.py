"""
zdrovena.month_closing.download_watcher – Interactive download watcher
========================================================================
Opens a fallback URL in the user's browser and watches ~/Downloads for
matching files (by glob pattern).  Used for vendors that cannot be
automated (e.g. Google Ads, Canva manual fallback).

Extracted from ``orchestrator.py`` to keep the orchestrator focused on
pipeline sequencing.
"""

from __future__ import annotations

import time
import webbrowser
from pathlib import Path

from zdrovena.month_closing.config import (
    DOWNLOAD_WATCH_DIR,
    DOWNLOAD_WATCH_POLL,
    DOWNLOAD_WATCH_TIMEOUT,
    VendorConfig,
)
from zdrovena.month_closing.console import ConsoleReporter


def interactive_download(
    vendors: list[VendorConfig],
    out: ConsoleReporter,
    on_match: object | None = None,
) -> list[tuple[VendorConfig, Path]]:
    """
    Open fallback URLs and watch ~/Downloads for matching files.

    Parameters
    ----------
    vendors   : Vendor configs that have ``fallback_url`` + ``download_glob``.
    out       : ConsoleReporter for structured output.
    on_match  : Optional callback ``(vendor, path) → None`` invoked on each match.

    Returns
    -------
    list of ``(VendorConfig, Path)`` pairs for each successfully detected file.
    """
    watch_dir = DOWNLOAD_WATCH_DIR
    resolved: list[tuple[VendorConfig, Path]] = []

    for vendor in vendors:
        url = vendor.fallback_url
        glob_pat = vendor.download_glob
        assert url and glob_pat  # guaranteed by caller filter

        # Pre-validation: file may already be in ~/Downloads
        existing = sorted(
            (f for f in watch_dir.glob(glob_pat) if f.is_file()),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        ) if watch_dir.exists() else []

        if existing:
            found = existing[0]
            size = found.stat().st_size
            out.plain(
                f"  ✅ {vendor.name}: {found.name} already in "
                f"~/Downloads ({size:,} bytes)"
            )
            resolved.append((vendor, found))
            if on_match:
                on_match(vendor, found)  # type: ignore[operator]
            continue

        # Show download box
        out.plain()
        out.plain(
            "  ╔══════════════════════════════════════════════════════╗"
        )
        out.plain(
            f"  ║  {vendor.name}: manual download required             "
            .ljust(56) + "║"
        )
        out.plain(
            "  ╚══════════════════════════════════════════════════════╝"
        )
        out.plain(f"  🔗 {url}")
        out.plain("  📂 Save to: ~/Downloads")
        out.plain(
            f"  ⏳ Watching for: {glob_pat} "
            f"(timeout {DOWNLOAD_WATCH_TIMEOUT}s)"
        )
        out.plain()

        # Snapshot existing files before opening browser
        before: set[Path] = set(watch_dir.glob(glob_pat)) if watch_dir.exists() else set()

        webbrowser.open(url)

        deadline = time.time() + DOWNLOAD_WATCH_TIMEOUT
        found_file: Path | None = None
        while time.time() < deadline:
            time.sleep(DOWNLOAD_WATCH_POLL)
            current: set[Path] = (
                set(watch_dir.glob(glob_pat)) if watch_dir.exists() else set()
            )
            new_files = current - before
            if new_files:
                found_file = max(new_files, key=lambda f: f.stat().st_mtime)
                break
            remaining = int(deadline - time.time())
            if remaining > 0 and remaining % 10 == 0:
                print(
                    f"\r  ⏳ Waiting… {remaining}s remaining",
                    end="", flush=True,
                )

        if found_file:
            size = found_file.stat().st_size
            print()  # clear the \r line
            out.plain(
                f"  ✅ {vendor.name}: {found_file.name} ({size:,} bytes)"
            )
            resolved.append((vendor, found_file))
            if on_match:
                on_match(vendor, found_file)  # type: ignore[operator]
        else:
            print()  # clear the \r line
            out.plain(
                f"  ⚠️  {vendor.name}: timed out — no file detected"
            )

    return resolved
