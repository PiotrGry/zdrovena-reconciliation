"""
zdrovena.month_closing.commands.preflight_cmd – CLI handler for ``zdrovena preflight``
======================================================================================

Runs only the pre-flight check: searches Zoho Mail for vendor invoices,
prints download links for missing files, and checks for bank statement.
Does NOT start the month-close pipeline.
"""

from __future__ import annotations

import argparse
import calendar
import logging
import re
import sys


def _parse_month(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d{4})-(\d{2})", value)
    if not match:
        raise argparse.ArgumentTypeError(
            f"Invalid format: {value!r}. Expected YYYY-MM (e.g. 2025-06)."
        )
    year, month = int(match.group(1)), int(match.group(2))
    if not (1 <= month <= 12):
        raise argparse.ArgumentTypeError(f"Month must be 01–12, got {month:02d}.")
    if year < 2020 or year > 2099:
        raise argparse.ArgumentTypeError(f"Year out of range: {year}.")
    return year, month


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    sp = subparsers.add_parser(
        "preflight",
        help="Pre-flight check: find missing invoices, print download links",
        description=(
            "Runs the pre-flight check for month closing:\n"
            "  - Searches Zoho Mail for Canva, Google Ads and other vendor invoices\n"
            "  - Prints download links for missing files\n"
            "  - Checks for bank statement in inbox folder\n"
            "  - Does NOT start the close pipeline\n\n"
            "Download missing files to inbox/, then run: zdrovena close YYYY-MM"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Przykłady:\n"
            "  zdrovena preflight 2025-06           # check what's missing for June\n"
            "  zdrovena preflight --period 2025-06   # same, with flag\n"
        ),
    )
    sp.add_argument(
        "period",
        type=str,
        nargs="?",
        help="Miesiąc w formacie YYYY-MM (np. 2025-06). Można też użyć --period.",
    )
    sp.add_argument(
        "--period",
        type=str,
        dest="period_flag",
        help="Miesiąc w formacie YYYY-MM. Alternatywa dla pozycyjnego argumentu.",
    )
    sp.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Szczegółowe logowanie (DEBUG)",
    )
    sp.add_argument(
        "--no-browser",
        action="store_true",
        help="Skip Playwright auto-download of Fakturownia reports",
    )
    sp.set_defaults(func=_run)


def _run(args: argparse.Namespace) -> None:
    pos_period = getattr(args, "period", None)
    flag_period = getattr(args, "period_flag", None)
    if pos_period and flag_period and pos_period != flag_period:
        print(f"❌ Podano dwa różne okresy: '{pos_period}' i '{flag_period}'. Użyj jednego.", file=sys.stderr)
        sys.exit(1)
    period_value = flag_period or pos_period
    if not period_value:
        print("❌ Musisz podać miesiąc w formacie YYYY-MM", file=sys.stderr)
        sys.exit(1)
    try:
        year, month = _parse_month(period_value)
    except argparse.ArgumentTypeError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        sys.exit(1)

    verbose = getattr(args, "verbose", False)
    if verbose:
        logging.basicConfig(level=logging.DEBUG, format="%(name)s %(message)s")
    else:
        logging.basicConfig(level=logging.INFO, format="%(message)s")

    from zdrovena.common.formatting import MONTHS_FULL
    from zdrovena.month_closing.config import BASE_DIR, DOWNLOAD_WATCH_DIR
    from zdrovena.month_closing.preflight import PreflightChecker

    month_dir = BASE_DIR / str(year) / MONTHS_FULL[month]

    # Date ranges as strings (PreflightChecker expects "YYYY-MM-DD" strings)
    date_from = f"{year}-{month:02d}-01"
    last_day = calendar.monthrange(year, month)[1]
    date_to = f"{year}-{month:02d}-{last_day:02d}"  # for Fakturownia reports
    if month == 12:
        cost_date_to = f"{year + 1}-01-01"
    else:
        cost_date_to = f"{year}-{month + 1:02d}-01"

    print(f"\n🔍 Pre-flight check for {year}-{month:02d}\n")
    print(f"   Inbox folder: {DOWNLOAD_WATCH_DIR}")
    print()

    try:
        no_browser = getattr(args, "no_browser", False)
        checker = PreflightChecker(
            year=year,
            month=month,
            month_dir=month_dir,
            date_from=date_from,
            date_to=date_to,
            cost_date_to=cost_date_to,
            dry_run=True,
            get_secret=_get_secret,
            no_browser=no_browser,
        )
        result = checker.run()
    except KeyboardInterrupt:
        print("\n⏹ Przerwano.")
        sys.exit(130)
    except Exception as exc:
        print(f"\n❌ Pre-flight failed: {exc}", file=sys.stderr)
        sys.exit(1)

    # Summary
    print()
    if result.missing_vendors or result.missing_reports or not result.bank_statement_found:
        missing = [v.name for v in result.missing_vendors]
        missing_reports = [r["name"] for r in result.missing_reports]
        parts = []
        if missing:
            parts.append(f"missing invoices: {', '.join(missing)}")
        if missing_reports:
            parts.append(f"missing reports: {', '.join(missing_reports)}")
        if not result.bank_statement_found:
            parts.append("bank statement not found")
        print(f"⚠️  {'; '.join(parts)}")
        print(f"\n   Download files to: {DOWNLOAD_WATCH_DIR}")
        print(f"   Then run: zdrovena close {year}-{month:02d}")
        sys.exit(1)
    else:
        print(f"✅ All files ready for {year}-{month:02d}")
        print(f"   Run: zdrovena close {year}-{month:02d}")


def _get_secret(service: str, required: bool = True) -> str | None:
    """Resolve secret from env var, then .env, then keyring."""
    import os
    env_key = service.upper().replace(".", "_").replace("-", "_")
    val = os.environ.get(env_key)
    if val:
        return val
    # Try .env file
    try:
        from pathlib import Path
        env_file = Path(__file__).resolve().parents[3] / ".env"
        if env_file.is_file():
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == env_key:
                    return v.strip()
    except Exception:
        pass
    # Keyring fallback
    try:
        import keyring
        val = keyring.get_password(service, "humio")
        if val:
            return val
    except Exception:
        pass
    if required:
        raise RuntimeError(f"Missing secret: {service} (set env var {env_key})")
    return None


