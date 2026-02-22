"""
``zdrovena audit`` – Full audit: WZ vs FV analysis.

Multi-section analysis covering: manual recount, type-level match,
orphan WZ, invoices without WZ, date comparisons, cross-month analysis,
numbering continuity, stock balance, anomalies, and a final PASSED/FAILED verdict.

Section logic lives in :mod:`zdrovena.audit.sections`; this module
provides the CLI entry-point, the :class:`Verdict` tracker, and the
orchestration that wires everything together.
"""

from __future__ import annotations

import argparse
import sys

from zdrovena.audit.api import (
    get_client, fetch_invoices, fetch_wz_documents,
    fetch_warehouse_actions, fetch_all_warehouse_actions, fetch_products,
    build_actions_by_doc, build_wz_by_id,
    build_inv_by_wz,
)
from zdrovena.audit.sections import (
    section_recount,
    section_type_match,
    section_orphan_wz,
    section_no_wz,
    section_date_comparison,
    section_cross_month_sell_issue,
    section_numbering,
    section_stock_balance,
    section_anomalies,
)
from zdrovena.common.formatting import (
    SEP, BOLD, RESET, GREEN, RED,
)


# ── Verdict tracker ──────────────────────────────────────────────────────────

class Verdict:
    """Accumulates pass/fail results across sections."""

    def __init__(self) -> None:
        self._issues: list[str] = []

    def fail(self, msg: str) -> None:
        self._issues.append(msg)

    @property
    def passed(self) -> bool:
        return len(self._issues) == 0

    @property
    def issues(self) -> list[str]:
        return list(self._issues)


# ── Subcommand ────────────────────────────────────────────────────────────────

def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "audit",
        help="Pełny audyt: WZ vs FV — analiza rozbieżności",
        description="Wielosekcyjny audyt porównujący faktury, WZ, daty i anomalie.",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> None:
    client = get_client()
    print("Pobieranie danych...")

    invoices = fetch_invoices(client, args.year, include_proforma=False)
    wz_docs = fetch_wz_documents(client, args.year)
    wz_actions = fetch_warehouse_actions(client)
    all_actions = fetch_all_warehouse_actions(client)
    products = fetch_products(client)

    print(f"  Pobrano: {len(invoices)} faktur, {len(wz_docs)} WZ, "
          f"{len(wz_actions)} akcji WZ, {len(all_actions)} akcji łącznie, "
          f"{len(products)} produktów")

    wz_by_id = build_wz_by_id(wz_docs)
    doc_actions = build_actions_by_doc(wz_actions)
    inv_by_wz = build_inv_by_wz(invoices, wz_by_id)

    verdict = Verdict()

    # §1 Recount (total)
    print(f"\n{SEP}")
    print("1. RĘCZNE LICZENIE KAŻDEJ FAKTURY (pozycje butelkowe)")
    print(SEP)
    grand_fv, grand_wz, _, _ = section_recount(inv_by_wz, doc_actions, verdict)

    # §2 Type-level match (plastik/szkło)
    print(f"\n{SEP}")
    print("2. ZGODNOŚĆ TYPÓW: plastik FV = plastik WZ, szkło FV = szkło WZ")
    print(SEP)
    section_type_match(inv_by_wz, doc_actions, verdict)

    # §3 Orphan WZ
    print(f"\n{SEP}")
    print("3. WZ BEZ FAKTUR (orphan WZ)")
    print(SEP)
    section_orphan_wz(wz_docs, inv_by_wz, doc_actions, verdict)

    # §4 Invoices without WZ
    print(f"\n{SEP}")
    print("4. FAKTURY BEZ WZ (mają pozycje butelkowe ale brak warehouse_document_id)")
    print(SEP)
    no_wz = section_no_wz(invoices, inv_by_wz, verdict)

    # §5 Date comparison
    print(f"\n{SEP}")
    print("5. PORÓWNANIE DAT: sell_date FV vs issue_date WZ")
    print(SEP)
    section_date_comparison(inv_by_wz, wz_by_id, verdict)

    # §6 sell_date vs issue_date on invoice
    print(f"\n{SEP}")
    print("6. sell_date vs issue_date NA FAKTURZE (cross-month)")
    print(SEP)
    section_cross_month_sell_issue(invoices, verdict)

    # §7 Numbering continuity
    print(f"\n{SEP}")
    print("7. CIĄGŁOŚĆ NUMERACJI (luki i duplikaty per seria)")
    print(SEP)
    section_numbering(invoices, verdict)

    # §8 Stock balance
    print(f"\n{SEP}")
    print("8. BILANS MAGAZYNOWY (ΣPZ − ΣWZ = warehouse_quantity)")
    print(SEP)
    section_stock_balance(all_actions, products, verdict)

    # §9 Anomalies
    print(f"\n{SEP}")
    print("9. ANOMALIE")
    print(SEP)
    section_anomalies(inv_by_wz, wz_by_id, invoices)

    # §10 Final verdict
    print(f"\n{SEP}")
    print("10. PODSUMOWANIE")
    print(SEP)

    orphan_count = len([w for w in wz_docs if w["id"] not in inv_by_wz])
    print(f"""
  FV (z pozycji faktur):     {grand_fv}
  WZ (z dokumentów mag.):    {grand_wz}
  Różnica FV−WZ:             {grand_fv - grand_wz:+d}

  Faktur z WZ:               {len(inv_by_wz)}
  Faktur bez WZ (z btl):     {len(no_wz)}
  WZ bez faktury:            {orphan_count}
""")

    if verdict.passed:
        print(f"  {BOLD}{GREEN}████  PASSED  ████{RESET}\n")
    else:
        print(f"  {BOLD}{RED}████  FAILED  ████{RESET}")
        for issue in verdict.issues:
            print(f"  {RED}  ✗ {issue}{RESET}")
        print()
        sys.exit(1)
