"""
zdrovena.month_closing.commands.close_cmd – CLI handler for ``zdrovena close``
================================================================================
"""

from __future__ import annotations

import argparse
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
        "close",
        help="Zamknięcie miesiąca – pełny pipeline księgowy",
        description=(
            "Automatyczne zamknięcie miesiąca:\n"
            "  0. Pre-flight (sprawdzenie dokumentów)\n"
            "  1. Tworzenie struktury folderów\n"
            "  2. Pobieranie faktur sprzedaży (Fakturownia)\n"
            "  3. Weryfikacja JPK + VAT\n"
            "  4. Pobieranie faktur kosztowych (KSeF → Fakturownia → Zoho Mail)\n"
            "  5. Weryfikacja wyciągu bankowego\n"
            "  6. Tworzenie archiwum ZIP\n"
            "  7. Wysyłka e-mail do księgowej"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Przykłady:\n"
            "  zdrovena close 2025-06             # pełny pipeline\n"
            "  zdrovena close 2025-06 --dry-run   # symulacja\n"
            "  zdrovena close 2025-06 --zip       # tylko ZIP\n"
            "  zdrovena close 2025-06 --zip --send  # ZIP + wysyłka\n"
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
        help="Miesiąc w formacie YYYY-MM (np. 2025-06). Alternatywa dla pozycyjnego argumentu.",
    )
    sp.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Tryb symulacji — bez zapisu plików i wysyłki",
    )
    sp.add_argument(
        "--zip",
        action="store_true",
        default=False,
        help="Tylko tworzenie archiwum ZIP (pomija e-mail)",
    )
    sp.add_argument(
        "--send",
        action="store_true",
        default=False,
        help="Tylko wysyłka e-mail (wymaga istniejącego ZIP)",
    )
    sp.add_argument(
        "--reset",
        action="store_true",
        default=False,
        help="Reset stanu pipeline — wszystkie kroki zostaną ponownie wykonane",
    )
    sp.add_argument(
        "--verbose", "-v",
        action="store_true",
        default=False,
        help="Szczegółowe logowanie (DEBUG)",
    )
    sp.add_argument(
        "--non-interactive",
        action="store_true",
        default=False,
        help="Tryb nieinteraktywny — pomiń ręczne pobieranie, wymaga plików już obecnych",
    )
    sp.add_argument(
        "--ignore-warnings",
        action="store_true",
        default=False,
        help="Kontynuuj tworzenie ZIP mimo ostrzeżeń (wysyłka e-mail nadal zablokowana)",
    )
    sp.set_defaults(func=_run)


def _configure_logging(verbose: bool = False) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)

    fmt = logging.Formatter(
        "[%(asctime)s] %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG if verbose else logging.INFO)
    ch.setFormatter(fmt)
    root.addHandler(ch)

    fh = logging.FileHandler("close_month.log", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    root.addHandler(fh)


def _run(args: argparse.Namespace) -> None:
    pos_period = getattr(args, "period", None)
    flag_period = getattr(args, "period_flag", None)
    if pos_period and flag_period and pos_period != flag_period:
        print(f"❌ Podano dwa różne okresy: '{pos_period}' (pozycyjny) i '{flag_period}' (--period). Użyj jednego.", file=sys.stderr)
        sys.exit(1)
    period_value = flag_period or pos_period
    if not period_value:
        print("❌ Musisz podać miesiąc w formacie YYYY-MM jako argument pozycyjny lub --period YYYY-MM", file=sys.stderr)
        sys.exit(1)
    try:
        year, month = _parse_month(period_value)
    except argparse.ArgumentTypeError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        sys.exit(1)

    verbose = getattr(args, "verbose", False)
    _configure_logging(verbose=verbose)
    logger = logging.getLogger("zdrovena.close")

    dry_run = getattr(args, "dry_run", False)
    do_zip = getattr(args, "zip", False)
    do_send = getattr(args, "send", False)
    do_reset = getattr(args, "reset", False)
    non_interactive = getattr(args, "non_interactive", False)
    ignore_warnings = getattr(args, "ignore_warnings", False)

    zip_only = do_zip and not do_send
    send_only = do_send and not do_zip
    zip_and_send = do_zip and do_send
    full_pipeline = not do_zip and not do_send

    mode_label = (
        "zip-only"
        if zip_only
        else "send-only"
        if send_only
        else "zip+send"
        if zip_and_send
        else f"full (dry_run={dry_run})"
    )
    logger.info("Starting monthly close for %04d-%02d (%s)", year, month, mode_label)

    from zdrovena.month_closing.orchestrator import MonthCloseOrchestrator

    try:
        orchestrator = MonthCloseOrchestrator(
            year=year,
            month=month,
            dry_run=dry_run,
            non_interactive=non_interactive,
            ignore_warnings=ignore_warnings,
        )

        if do_reset:
            orchestrator.state.reset()
            logger.info("Pipeline state reset — all steps will re-run")

        if full_pipeline:
            report = orchestrator.execute()
        elif zip_and_send:
            report = orchestrator.execute_zip_and_send()
        elif zip_only:
            report = orchestrator.execute_zip_only()
        elif send_only:
            report = orchestrator.execute_send_only()
        else:
            report = orchestrator.execute()

    except KeyboardInterrupt:
        print("\n⏹ Przerwano przez użytkownika.")
        sys.exit(130)
    except SystemExit:
        raise
    except Exception as exc:
        logger.critical("Fatal error: %s", exc, exc_info=True)
        print(f"\n❌ FATAL: {exc}")
        sys.exit(1)

    if report.errors:
        sys.exit(1)
    sys.exit(0)
