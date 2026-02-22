#!/usr/bin/env python3
"""
Zdrovena CLI – invoice audit, bottle tracking & month-close.

Usage::

    zdrovena -y 2025 audit
    zdrovena -y 2025 -m 6 list
    zdrovena -y 2025 export
    zdrovena -y 2025 summary
    zdrovena products
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from zdrovena.audit.commands import audit_cmd, list_cmd, export, summary, products, report_cmd
from zdrovena.month_closing.commands import close_cmd, setup_cmd


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="zdrovena",
        description=(
            "Zdrovena Reconciliation – audyt faktur, śledzenie butelek "
            "i zamknięcie miesiąca (Humio)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Przykłady:\n"
            "  zdrovena -y 2025 audit                # pełny audyt + PASSED/FAILED\n"
            "  zdrovena -y 2025 -m 6 list             # faktury z czerwca 2025\n"
            "  zdrovena -y 2025 -m 6 -d 15 list       # konkretny dzień\n"
            "  zdrovena -y 2025 export                 # eksport CSV per miesiąc\n"
            "  zdrovena -y 2025 summary                # WZ vs FV\n"
            "  zdrovena products                       # lista produktów\n"
            "  zdrovena close 2025-06                  # zamknięcie miesiąca\n"
            "  zdrovena close 2025-06 --dry-run        # symulacja\n"
            "  zdrovena setup                           # wizard credentiali\n"
            "  zdrovena setup --check                   # sprawdź sekrety\n"
        ),
    )

    # Global arguments
    parser.add_argument(
        "--year", "-y",
        type=int,
        default=date.today().year,
        help="Rok (domyślnie: bieżący rok)",
    )
    parser.add_argument(
        "--month", "-m",
        type=int,
        default=None,
        help="Miesiąc 1-12 (domyślnie: cały rok)",
    )
    parser.add_argument(
        "--day", "-d",
        type=int,
        default=None,
        help="Dzień (opcjonalny — wymaga --month)",
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

    # Register subcommands
    audit_cmd.add_subparser(subparsers)
    list_cmd.add_subparser(subparsers)
    export.add_subparser(subparsers)
    summary.add_subparser(subparsers)
    products.add_subparser(subparsers)
    report_cmd.add_subparser(subparsers)
    close_cmd.add_subparser(subparsers)
    setup_cmd.add_subparser(subparsers)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # Validate --day requires --month (for commands that use it)
    if args.day and not args.month:
        parser.error("--day wymaga --month")

    args.func(args)


if __name__ == "__main__":
    main()
