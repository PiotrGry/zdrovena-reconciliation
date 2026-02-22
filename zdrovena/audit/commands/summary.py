"""
``zdrovena summary`` – WZ vs FV summary table.
"""

from __future__ import annotations

import argparse
from collections import defaultdict

from zdrovena.audit.api import (
    get_client, fetch_invoices, fetch_wz_documents,
    fetch_warehouse_actions, build_actions_by_doc, build_wz_by_id,
    build_inv_by_wz, month_of, sell_date_of,
)
from zdrovena.audit.bottles import extract_bottles, BOTTLE_ALIASES, BOTTLE_PRODUCTS
from zdrovena.audit.sections import check_numbering
from zdrovena.common.formatting import MONTHS_PL, BOLD, RESET, GREEN, YELLOW, RED


def add_subparser(subparsers: argparse._SubParsersAction, *, parents: list | None = None) -> None:
    p = subparsers.add_parser(
        "summary",
        parents=parents or [],
        help="Tabela WZ vs FV (plastik/szkło) per miesiąc",
        description=(
            "Kompaktowe porównanie magazynu (WZ) z fakturami\n"
            "z podziałem plastik/szkło per miesiąc.\n\n"
            "Przykłady:\n"
            "  zdrovena summary -y 2026             # cały rok"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> None:
    client = get_client()
    print("Pobieranie danych...")

    all_inv = fetch_invoices(client, args.year, include_proforma=False)
    wz_docs = fetch_wz_documents(client, args.year)
    actions = fetch_warehouse_actions(client)

    actions_by_doc = build_actions_by_doc(actions)
    wz_by_id = build_wz_by_id(wz_docs)
    inv_by_wz = build_inv_by_wz(all_inv, wz_by_id)

    # Monthly aggregation by sell_date
    m_wz: dict[int, list[int]] = defaultdict(lambda: [0, 0])
    m_fv: dict[int, list[int]] = defaultdict(lambda: [0, 0])

    for wd in wz_docs:
        linked = inv_by_wz.get(wd["id"])
        if not linked:
            continue

        sell = sell_date_of(linked)
        month = month_of(sell)
        if month == 0:
            continue

        # WZ actions
        for a in actions_by_doc.get(wd["id"], []):
            pname = BOTTLE_ALIASES.get(a.get("product_name", ""), a.get("product_name", ""))
            if pname in BOTTLE_PRODUCTS:
                q = int(abs(float(a["quantity"])))
                if "szkło" in pname.lower():
                    m_wz[month][1] += q
                else:
                    m_wz[month][0] += q

        # FV positions (regex)
        for pos in linked.get("positions", []):
            p, g = extract_bottles(pos.get("name", ""), float(pos.get("quantity", 0)))
            m_fv[month][0] += p
            m_fv[month][1] += g

    # Render table
    print()
    print(f"{'':>4}  {'WZ_p':>6} {'WZ_g':>6} {'WZ':>7}  │  "
          f"{'FV_p':>6} {'FV_g':>6} {'FV':>7}  │  {'Δ':>4}")
    print("─" * 65)

    tw = [0, 0]
    tf = [0, 0]

    for m in range(2, 13):
        wp, wg = m_wz[m]
        wt = wp + wg
        fp, fg = m_fv[m]
        ft = fp + fg
        d = wt - ft
        flag = "✅" if d == 0 else "❌"

        print(f"{MONTHS_PL[m]:>4}  {wp:>6} {wg:>6} {wt:>7}  │  "
              f"{fp:>6} {fg:>6} {ft:>7}  │  {d:>+4} {flag}")

        tw[0] += wp
        tw[1] += wg
        tf[0] += fp
        tf[1] += fg

    print("─" * 65)
    wt = tw[0] + tw[1]
    ft = tf[0] + tf[1]
    print(f"{'ROK':>4}  {tw[0]:>6} {tw[1]:>6} {wt:>7}  │  "
          f"{tf[0]:>6} {tf[1]:>6} {ft:>7}  │  {wt - ft:>+4}")

    # Numbering check
    print()
    print(f"{BOLD}Numeracja faktur:{RESET}")
    results = check_numbering(all_inv)
    for sr in results:
        if sr.ok:
            print(f"  /{sr.series}:  {sr.first}–{sr.last} ({sr.count} dok.) ✅")
        else:
            print(f"  /{sr.series}:  {sr.first}–{sr.last}, "
                  f"jest {sr.count}, oczekiwano {sr.expected}")
            if sr.gaps:
                print(f"    {RED}❌ Brakuje: {sr.gaps}{RESET}")
            if sr.duplicates:
                print(f"    {RED}❌ Duplikaty: {sr.duplicates}{RESET}")
    print()
