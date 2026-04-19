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
    build_actions_by_doc,
    build_inv_by_wz,
    build_wz_by_id,
    fetch_all_warehouse_actions,
    fetch_invoices,
    fetch_products,
    fetch_warehouse_actions,
    fetch_wz_documents,
    get_client,
)
from zdrovena.audit.sections import (
    section_anomalies,
    section_cross_month_sell_issue,
    section_date_comparison,
    section_no_wz,
    section_numbering,
    section_orphan_wz,
    section_recount,
    section_stock_balance,
    section_type_match,
)
from zdrovena.common.formatting import (
    BOLD,
    GREEN,
    RED,
    RESET,
    SEP,
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


def add_subparser(subparsers: argparse._SubParsersAction, *, parents: list | None = None) -> None:
    p = subparsers.add_parser(
        "audit",
        parents=parents or [],
        help="Pełny audyt butelek: FV vs WZ, daty, numeracja, magazyn",
        description=(
            "Wielosekcyjny audyt butelek — porównuje faktury z dokumentami\n"
            "magazynowymi WZ, sprawdza daty, ciągłość numeracji i bilans.\n\n"
            "Przykłady:\n"
            "  zdrovena audit  -y 2026          # cały rok\n"
            "  zdrovena audit  -y 2026 -m 02    # tylko luty"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> None:
    client = get_client()
    print("Pobieranie danych...")

    invoices = fetch_invoices(client, args.year, month=args.month, include_proforma=False)
    wz_docs = fetch_wz_documents(client, args.year, month=args.month)
    wz_actions = fetch_warehouse_actions(client)
    all_actions = fetch_all_warehouse_actions(client)
    products = fetch_products(client)

    print(
        f"  Pobrano: {len(invoices)} faktur, {len(wz_docs)} WZ, "
        f"{len(wz_actions)} akcji WZ, {len(all_actions)} akcji łącznie, "
        f"{len(products)} produktów"
    )

    wz_by_id = build_wz_by_id(wz_docs)
    doc_actions = build_actions_by_doc(wz_actions)
    inv_by_wz = build_inv_by_wz(invoices, wz_by_id)

    verdict = Verdict()

    # §1 Recount (total)
    print(f"\n{SEP}")
    print("1. RĘCZNE LICZENIE KAŻDEJ FAKTURY (pozycje butelkowe)")
    print(SEP)
    grand_fv, grand_wz, _, _ = section_recount(inv_by_wz, doc_actions, verdict, month=args.month)

    # §2 Quick checks (type match, orphans, dates)
    print(f"\n{SEP}")
    print("2. KONTROLE")
    print(SEP)
    section_type_match(inv_by_wz, doc_actions, verdict)
    section_orphan_wz(wz_docs, inv_by_wz, doc_actions, verdict)
    no_wz = section_no_wz(invoices, inv_by_wz, verdict)
    section_date_comparison(inv_by_wz, wz_by_id, verdict)
    section_cross_month_sell_issue(invoices, verdict)

    # §3 Numbering & stock balance
    print(f"\n{SEP}")
    print("3. NUMERACJA I MAGAZYN")
    print(SEP)
    section_numbering(invoices, verdict)
    print()
    section_stock_balance(all_actions, products, args.year, args.month, verdict)

    # §4 Anomalies
    print(f"\n{SEP}")
    print("4. ANOMALIE")
    print(SEP)
    section_anomalies(inv_by_wz, wz_by_id, invoices)

    # §5 Final verdict
    print(f"\n{SEP}")
    print("5. PODSUMOWANIE")
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
