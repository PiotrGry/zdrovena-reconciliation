"""
``zdrovena export`` – Export per-month invoice CSVs.
"""

from __future__ import annotations

import argparse
import csv
import os
from collections import defaultdict

from zdrovena.audit.api import (
    get_client, fetch_invoices, month_of, sell_date_of, doc_type_label, inv_sort_key,
)
from zdrovena.audit.bottles import invoice_bottle_details
from zdrovena.common.formatting import MONTHS_FULL, MONTHS_PL, BOLD, RESET, GREEN, DIM


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "export",
        help="Eksportuj pozycje butelkowe do plików CSV (per miesiąc)",
        description="Generuje pliki CSV z pozycjami butelkowymi, pogrupowane wg miesiąca.",
    )
    p.add_argument(
        "--output-dir", "-o",
        type=str, default="csv",
        help="Katalog docelowy na pliki CSV (domyślnie: csv/)",
    )
    p.add_argument(
        "--from-month", type=int, default=2, metavar="M",
        help="Miesiąc początkowy (domyślnie: 2)",
    )
    p.add_argument(
        "--to-month", type=int, default=12, metavar="M",
        help="Miesiąc końcowy (domyślnie: 12)",
    )
    p.add_argument(
        "--single-month", type=int, default=None, metavar="M",
        help="Eksportuj tylko ten miesiąc (nadpisuje --from/--to)",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> None:
    client = get_client()
    print("Pobieranie faktur...")
    invoices = fetch_invoices(client, args.year, include_proforma=False)

    by_month: dict[int, list[dict]] = defaultdict(list)
    for inv in invoices:
        m = month_of(sell_date_of(inv))
        if m:
            by_month[m].append(inv)

    outdir = args.output_dir
    os.makedirs(outdir, exist_ok=True)

    if args.single_month:
        months = [args.single_month]
    else:
        months = list(range(args.from_month, args.to_month + 1))

    for month in months:
        invs = sorted(by_month.get(month, []), key=inv_sort_key)

        rows: list[tuple] = []
        total_btl = 0
        for inv in invs:
            btl, details = invoice_bottle_details(inv)
            total_btl += btl
            sell = sell_date_of(inv)
            nr = inv["number"]
            m_name = MONTHS_FULL.get(month, str(month))
            kind = doc_type_label(inv)
            if details:
                names = " + ".join(name for name, qty, bpu, cnt in details)
                rows.append((sell, m_name, nr, kind, names, btl))

        fname = os.path.join(outdir, f"{MONTHS_FULL.get(month, str(month))}_{args.year}.csv")
        with open(fname, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["LP", "miesiac", "nr", "typ", "data sprz", "nazwa", "ilosc"])
            for lp, (sell, m_name, nr, kind, name, cnt) in enumerate(rows, 1):
                w.writerow([lp, m_name, nr, kind, sell, name, cnt])
            w.writerow([])
            par_count = sum(1 for r in rows if r[3] == "PAR")
            fv_count = len(rows) - par_count
            w.writerow(["", "", "", "", "", "Liczba dokumentów:", len(rows)])
            if par_count:
                w.writerow(["", "", "", "", "", "  w tym FV:", fv_count])
                w.writerow(["", "", "", "", "", "  w tym PAR:", par_count])
            w.writerow(["", "", "", "", "", "Suma butelek:", total_btl])

        label = MONTHS_PL.get(month, f"{month:02d}")
        print(f"  {GREEN}✓{RESET} {label}: {total_btl:>5} butelek, "
              f"{len(invs)} faktur → {BOLD}{fname}{RESET}")

    print(f"\n{DIM}Pliki CSV zapisano w katalogu: {outdir}/{RESET}")
