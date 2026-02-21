"""
zdrovena.month_closing.console – Console Reporter
====================================================
Centralised, structured console output for the close-month pipeline.
"""

from __future__ import annotations

import logging
import sys
from typing import TextIO

logger = logging.getLogger("zdrovena.month_closing.console")


class ConsoleReporter:
    def __init__(self, stream: TextIO | None = None) -> None:
        self._out: TextIO = stream or sys.stdout

    def banner(self, text: str) -> None:
        width = max(len(text) + 6, 64)
        self._print()
        self._print("═" * width)
        self._print(f"  {text}")
        self._print("═" * width)

    def step(self, n: int, title: str, total: int = 7) -> None:
        self._print(f"\n▸ [{n}/{total}] {title}")

    def ok(self, msg: str) -> None:
        self._print(f"  ✅ {msg}")
        logger.info(msg)

    def warn(self, msg: str) -> None:
        self._print(f"  ⚠️  {msg}")
        logger.warning(msg)

    def error(self, msg: str) -> None:
        self._print(f"  ❌ {msg}")
        logger.error(msg)

    def skip(self, msg: str) -> None:
        self._print(f"  ⏭  {msg}")
        logger.info("Skipped: %s", msg)

    def info(self, msg: str) -> None:
        self._print(f"  ℹ️  {msg}")
        logger.info(msg)

    def item(self, msg: str) -> None:
        self._print(f"  │  {msg}")

    def detail(self, msg: str) -> None:
        self._print(f"     {msg}")

    def plain(self, msg: str = "") -> None:
        self._print(msg)

    def section_start(self, title: str) -> None:
        self._print(f"  ┌─ {title}")

    def section_mid(self, title: str) -> None:
        self._print(f"  ├─ {title}")

    def section_end(self, msg: str = "") -> None:
        self._print(f"  └─ {msg}" if msg else "  └─")

    def blocker_box(self, lines: list[str]) -> None:
        self._print()
        self._print("  ╔══════════════════════════════════════════════════════╗")
        self._print("  ║  MISSING DOCUMENTS — download and rerun the script   ║")
        self._print("  ╚══════════════════════════════════════════════════════╝")
        for line in lines:
            self._print(line)

    def summary_header(self, title: str) -> None:
        self._print()
        self._print("=" * 64)
        self._print(f"  📊  {title}")
        self._print("=" * 64)

    def summary_line(self, label: str, value: str) -> None:
        self._print(f"  {label:<22s}{value}")

    def summary_footer(self, success: bool) -> None:
        self._print()
        if success:
            self._print("  ✅  Monthly close completed successfully.")
        else:
            self._print("  ‼️  Close completed WITH ERRORS.")
        self._print("=" * 64)

    def _print(self, msg: str = "") -> None:
        print(msg, file=self._out)
