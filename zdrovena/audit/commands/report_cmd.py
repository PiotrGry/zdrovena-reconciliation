"""
``zdrovena report`` – Download Fakturownia UI reports.

Downloads reports (VAT sales, income, expenses, etc.) from the
Fakturownia web interface as PDF files using a headless browser.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from zdrovena.audit.report_downloader import (
    REPORT_KINDS,
    EmptyReportError,
    download_report,
)


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "report",
        help="Pobierz raport z Fakturowni (PDF via przeglądarka)",
        description=(
            "Pobiera raporty z interfejsu webowego Fakturowni jako pliki PDF.\n"
            "Wymaga: pip install playwright && playwright install chromium"
        ),
    )
    p.add_argument(
        "--kind", "-k",
        choices=list(REPORT_KINDS.keys()),
        default="vat-sales",
        help="Rodzaj raportu (domyślnie: vat-sales = Wykaz sprzedaży VAT)",
    )
    p.add_argument(
        "--output", "-o",
        type=str,
        default=None,
        help="Ścieżka do pliku wyjściowego (domyślnie: report_<kind>_<rok>-<mc>.pdf)",
    )
    p.add_argument(
        "--show-browser",
        action="store_true",
        default=False,
        help="Pokaż przeglądarkę (wyłącz tryb headless — do debugowania)",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> None:
    import calendar
    import logging
    import sys
    from datetime import date

    logging.basicConfig(level=logging.INFO, format="   %(message)s")

    year = args.year
    month = getattr(args, "month", None)

    # ── Validate: requested period must not be in the future ─────────
    today = date.today()
    if month:
        if date(year, month, 1) > today:
            print(f"❌ Okres {year}-{month:02d} jeszcze się nie rozpoczął (dziś: {today}).")
            sys.exit(1)
    else:
        if year > today.year:
            print(f"❌ Rok {year} jeszcze się nie rozpoczął (dziś: {today}).")
            sys.exit(1)

    if month:
        last_day = calendar.monthrange(year, month)[1]
        date_from = f"{year}-{month:02d}-01"
        date_to = f"{year}-{month:02d}-{last_day:02d}"
        period_label = f"{year}-{month:02d}"
    else:
        date_from = f"{year}-01-01"
        date_to = f"{year}-12-31"
        period_label = str(year)

    kind = args.kind

    if args.output:
        output_path = Path(args.output)
    else:
        output_path = Path.home() / "Downloads" / f"report_{kind}_{period_label}.pdf"

    print(f"📊 Pobieranie raportu: {kind}")
    print(f"   Okres: {date_from} → {date_to}")
    print(f"   Plik:  {output_path}")
    print()

    try:
        result = download_report(
            date_from=date_from,
            date_to=date_to,
            output_path=output_path,
            kind=kind,
            headless=not args.show_browser,
        )
    except EmptyReportError:
        print(f"\n⚠️  Raport pusty — brak danych za okres {date_from} → {date_to}.")
        sys.exit(1)

    size_kb = result.stat().st_size / 1024
    print(f"\n✅ Raport zapisany: {result} ({size_kb:.1f} KB)")
