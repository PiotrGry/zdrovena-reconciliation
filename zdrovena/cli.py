#!/usr/bin/env python3
"""
Zdrovena CLI – invoice audit, bottle tracking & month-close.

Usage::

    zdrovena audit -y 2025 -m 06
    zdrovena list  -y 2025 -m 06
    zdrovena export -y 2025
    zdrovena summary -y 2025
    zdrovena products
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root (secrets for Zoho, Fakturownia, KSeF)
_env_file = Path(__file__).resolve().parent.parent / ".env"
if _env_file.is_file():
    load_dotenv(_env_file)

from zdrovena.audit.commands import audit_cmd, list_cmd, export, summary, products, report_cmd
from zdrovena.month_closing.commands import close_cmd, preflight_cmd, setup_cmd
from zdrovena.api.commands import files_cmd, health_cmd


# ── Shared argument groups ────────────────────────────────────────────────────

def _period_parser() -> argparse.ArgumentParser:
    """Parent parser with -y / -m / -d flags (shared by audit subcommands)."""
    p = argparse.ArgumentParser(add_help=False)
    g = p.add_argument_group("okres")
    g.add_argument(
        "--year", "-y",
        type=int,
        default=date.today().year,
        metavar="YYYY",
        help="Rok (domyślnie: %(default)s)",
    )
    g.add_argument(
        "--month", "-m",
        type=int,
        default=None,
        metavar="MM",
        help="Miesiąc 01–12 (domyślnie: cały rok)",
    )
    g.add_argument(
        "--day", "-d",
        type=int,
        default=None,
        metavar="DD",
        help="Dzień (wymaga --month)",
    )
    return p


def main() -> None:
    period = _period_parser()

    parser = argparse.ArgumentParser(
        prog="zdrovena",
        description=(
            "Zdrovena Reconciliation – audyt faktur, śledzenie butelek "
            "i zamknięcie miesiąca (Humio)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Przykłady:\n"
            "  zdrovena audit  -y 2025              # pełny audyt roku\n"
            "  zdrovena audit  -y 2025 -m 06        # audyt czerwca\n"
            "  zdrovena list   -y 2025 -m 06        # faktury z czerwca\n"
            "  zdrovena list   -y 2025 -m 06 -d 15  # konkretny dzień\n"
            "  zdrovena export -y 2025               # eksport CSV\n"
            "  zdrovena summary -y 2025              # WZ vs FV\n"
            "  zdrovena report -y 2025 -m 06         # raport PDF\n"
            "  zdrovena products                     # lista produktów\n"
            "  zdrovena close 2025-06                # zamknięcie miesiąca\n"
            "  zdrovena close 2025-06 --dry-run      # symulacja\n"
            "  zdrovena setup                        # wizard credentiali\n"
            "  zdrovena setup --check                # sprawdź sekrety\n"
        ),
    )

    parser.add_argument(
        "--version", "-V",
        action="version",
        version="%(prog)s 2.0.0",
    )

    subparsers = parser.add_subparsers(
        title="polecenia",
        dest="command",
        description="Dostępne polecenia (użyj: zdrovena <polecenie> --help)",
    )

    # Register subcommands – audit family gets shared period args
    audit_cmd.add_subparser(subparsers, parents=[period])
    list_cmd.add_subparser(subparsers, parents=[period])
    export.add_subparser(subparsers, parents=[period])
    summary.add_subparser(subparsers, parents=[period])
    report_cmd.add_subparser(subparsers, parents=[period])
    products.add_subparser(subparsers)
    close_cmd.add_subparser(subparsers)
    preflight_cmd.add_subparser(subparsers)
    setup_cmd.add_subparser(subparsers)
    files_cmd.add_subparser(subparsers)
    health_cmd.add_subparser(subparsers)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # Validate --day requires --month (for commands that use it)
    if getattr(args, "day", None) and not getattr(args, "month", None):
        parser.error("--day wymaga --month")

    args.func(args)


if __name__ == "__main__":
    main()
