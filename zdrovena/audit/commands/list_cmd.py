"""
``zdrovena list`` – List invoices with bottle counts (plastic / glass).
"""

from __future__ import annotations

import argparse

from zdrovena.audit.api import get_client, fetch_invoices, doc_type_label, sell_date_of
from zdrovena.audit.bottles import extract_bottles
from zdrovena.common.formatting import (
    RESET, BOLD, DIM, CYAN, GREEN, YELLOW, MAGENTA,
)


def _print_table(invoices: list[dict], *, show_positions: bool = False) -> None:
    """Render an ANSI-colored table of invoices with bottle totals."""
    rows: list[dict] = []
    grand_plastic = grand_glass = 0

    for inv in invoices:
        inv_plastic = inv_glass = 0
        bottle_positions: list[str] = []

        for pos in inv.get("positions", []):
            name = pos.get("name", "")
            qty = float(pos.get("quantity", 0))
            plastic, glass = extract_bottles(name, qty)
            if plastic or glass:
                inv_plastic += plastic
                inv_glass += glass
                label = name.split("|")[0].strip()
                if len(label) > 45:
                    label = label[:42] + "..."
                bottle_positions.append(f"{label} ×{int(qty)}")

        if inv_plastic == 0 and inv_glass == 0:
            continue

        grand_plastic += inv_plastic
        grand_glass += inv_glass

        rows.append({
            "number": inv.get("number", "?"),
            "kind": doc_type_label(inv),
            "date": sell_date_of(inv) or "?",
            "buyer": (inv.get("buyer_name") or "—")[:30],
            "positions": bottle_positions,
            "plastic": inv_plastic,
            "glass": inv_glass,
            "total": inv_plastic + inv_glass,
        })

    if not rows:
        print(f"{YELLOW}Brak pozycji butelkowych w fakturach.{RESET}")
        return

    w_num = max(8, max(len(r["number"]) for r in rows))
    w_typ = 3
    w_date = 10
    w_buyer = max(10, max(len(r["buyer"]) for r in rows))
    w_col = 8

    sep = (
        f"+-{'-' * w_num}-+-{'-' * w_typ}-+-{'-' * w_date}-+-{'-' * w_buyer}-"
        f"+-{'-' * w_col}-+-{'-' * w_col}-+-{'-' * w_col}-+"
    )

    header = (
        f"| {BOLD}{'Faktura':<{w_num}}{RESET} "
        f"| {BOLD}{'Typ':<{w_typ}}{RESET} "
        f"| {BOLD}{'Data sprz.':<{w_date}}{RESET} "
        f"| {BOLD}{'Nabywca':<{w_buyer}}{RESET} "
        f"| {BOLD}{'Plastik':>{w_col}}{RESET} "
        f"| {BOLD}{'Szkło':>{w_col}}{RESET} "
        f"| {BOLD}{'Butelki':>{w_col}}{RESET} |"
    )

    print(f"\n{BOLD}{CYAN}📦 Zestawienie butelek{RESET}")
    print(sep)
    print(header)
    print(sep.replace("-", "="))

    for r in rows:
        plastic_s = f"{GREEN}{r['plastic']:>{w_col}}{RESET}" if r["plastic"] else f"{'—':>{w_col}}"
        glass_s = f"{MAGENTA}{r['glass']:>{w_col}}{RESET}" if r["glass"] else f"{'—':>{w_col}}"
        total_s = f"{BOLD}{r['total']:>{w_col}}{RESET}"

        kind_s = f"{YELLOW}{r['kind']}{RESET}" if r["kind"] == "PAR" else f"{r['kind']}"
        print(
            f"| {r['number']:<{w_num}} "
            f"| {kind_s:<{w_typ + (len(kind_s) - len(r['kind']))}s} "
            f"| {r['date']:<{w_date}} "
            f"| {r['buyer']:<{w_buyer}} "
            f"| {plastic_s} "
            f"| {glass_s} "
            f"| {total_s} |"
        )
        if show_positions:
            for p in r["positions"]:
                print(f"|   {DIM}{p}{RESET}")

    print(sep)
    grand_total = grand_plastic + grand_glass
    par_count = sum(1 for r in rows if r['kind'] == 'PAR')
    print(
        f"| {BOLD}{'RAZEM':<{w_num}}{RESET} "
        f"| {'':>{w_typ}} "
        f"| {'':>{w_date}} "
        f"| {'':>{w_buyer}} "
        f"| {BOLD}{GREEN}{grand_plastic:>{w_col}}{RESET} "
        f"| {BOLD}{MAGENTA}{grand_glass:>{w_col}}{RESET} "
        f"| {BOLD}{grand_total:>{w_col}}{RESET} |"
    )
    print(sep)
    print(f"\n  {GREEN}🧴 Plastik:{RESET} {grand_plastic}")
    print(f"  {MAGENTA}🍷 Szkło:{RESET}   {grand_glass}")
    print(f"  {BOLD}📦 Razem:{RESET}   {grand_total}")
    fv_count = len(rows) - par_count
    par_info = f" + {par_count} PAR" if par_count else ""
    print(f"  {DIM}Dokumentów z butelkami: {len(rows)} ({fv_count} FV{par_info}){RESET}\n")


def add_subparser(subparsers: argparse._SubParsersAction, *, parents: list | None = None) -> None:
    p = subparsers.add_parser(
        "list",
        parents=parents or [],
        help="Wyświetl faktury z liczbą butelek (plastik/szkło)",
        description=(
            "Pobiera faktury z Fakturowni i wyświetla tabelę\n"
            "z podziałem plastik/szkło.\n\n"
            "Przykłady:\n"
            "  zdrovena list -y 2026 -m 02       # faktury z lutego\n"
            "  zdrovena list -y 2026 -m 02 -d 15 # konkretny dzień\n"
            "  zdrovena list -y 2026 -m 02 -p    # z pozycjami"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--proforma", action="store_true", help="Uwzględnij proformy")
    p.add_argument("--positions", "-p", action="store_true", help="Pokaż pozycje butelkowe")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> None:
    from zdrovena.audit.api import date_range
    d_from, d_to = date_range(args.year, args.month, args.day)

    print(f"\n{BOLD}📄 Pobieranie dokumentów sprzedażowych z Fakturowni{RESET}")
    print(f"   Zakres sell_date: {d_from} → {d_to}\n")

    client = get_client()
    invoices = fetch_invoices(
        client, args.year, args.month, args.day,
        include_proforma=args.proforma,
    )

    if not invoices:
        print(f"{YELLOW}Brak faktur w tym okresie.{RESET}")
        return

    print(f"   Znaleziono {len(invoices)} faktur(y)\n")
    _print_table(invoices, show_positions=args.positions)
