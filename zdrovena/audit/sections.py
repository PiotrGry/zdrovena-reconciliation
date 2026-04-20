"""
zdrovena.audit.sections – Audit analysis sections
====================================================
Individual analysis sections used by the ``zdrovena audit`` command.

Each section function receives pre-fetched data (invoices, WZ documents,
warehouse actions) and a :class:`~zdrovena.audit.commands.audit_cmd.Verdict`
tracker.  Sections print their results directly and register failures
via ``verdict.fail()``.

Sections
--------
1. Recount (FV vs WZ totals per month)
2. Checks: type match, orphan WZ, missing WZ, dates, cross-month
3. Numbering continuity & stock balance
4. Anomalies (large invoices, multi-linked WZ)
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date as Date
from typing import TYPE_CHECKING

from zdrovena.audit.api import (
    doc_type_label,
    month_of,
    sell_date_of,
)
from zdrovena.audit.bottles import BOTTLE_ALIASES, BOTTLE_PRODUCTS, invoice_bottles, wz_bottles
from zdrovena.common.formatting import MONTHS_PL

if TYPE_CHECKING:
    from zdrovena.audit.commands.audit_cmd import Verdict


# ── §1 Recount ────────────────────────────────────────────────────────────────


def section_recount(
    inv_by_wz: dict[int, dict],
    doc_actions: dict[int, list[dict]],
    verdict: Verdict,
    month: int | None = None,
) -> tuple[int, int, dict[int, list[dict]], dict[int, int]]:
    """§1 — Recount every invoice and WZ by month (sell_date)."""
    month_invoices: dict[int, list[dict]] = defaultdict(list)
    month_wz: dict[int, int] = defaultdict(int)

    for _wz_id, inv in sorted(inv_by_wz.items(), key=lambda x: sell_date_of(x[1])):
        sell = sell_date_of(inv)
        m = month_of(sell)
        p, g = invoice_bottles(inv)
        total = p + g
        if total > 0:
            month_invoices[m].append(
                {
                    "number": inv.get("number", "?"),
                    "kind": inv.get("kind", "?"),
                    "sell_date": sell,
                    "issue_date": inv.get("issue_date", ""),
                    "plastic": p,
                    "glass": g,
                    "total": total,
                }
            )

    for wz_id, inv in inv_by_wz.items():
        m = month_of(sell_date_of(inv))
        p, g = wz_bottles(wz_id, doc_actions)
        month_wz[m] += p + g

    grand_fv = grand_wz = 0
    for m in sorted(month_invoices.keys()):
        items = month_invoices[m]
        mt = sum(i["total"] for i in items)
        wt = month_wz.get(m, 0)
        grand_fv += mt
        grand_wz += wt
        delta = mt - wt
        mark = "✅" if delta == 0 else f"Δ={delta:+d} ⚠️"

        print(f"\n  {MONTHS_PL.get(m, '?')} — {len(items)} dok., FV={mt}, WZ={wt} {mark}")
        print(f"  {'─' * 88}")
        print(
            f"  {'#':>4}  {'Numer':>18}  {'Typ':^5}  {'Data sprz.':^10}  {'Data wyst.':^10}  "
            f"{'Plastik':>7}  {'Szkło':>5}  {'Razem':>5}"
        )
        print(f"  {'─' * 88}")
        for i, item in enumerate(items, 1):
            kind_tag = "PAR" if item["kind"] == "receipt" else "VAT"
            print(
                f"  {i:4d}  {item['number']:>18s}  {kind_tag:^5}  "
                f"{item['sell_date']:^10}  {item['issue_date']:^10}  "
                f"{item['plastic']:7d}  {item['glass']:5d}  {item['total']:5d}"
            )
        print(f"  {'─' * 88}")

    print(f"\n  {'─' * 80}")
    delta = grand_fv - grand_wz
    period = MONTHS_PL.get(month, "?").upper() if month else "ROK"
    print(f"  RAZEM {period}:  FV={grand_fv}  WZ={grand_wz}  Δ={delta:+d}")

    if delta != 0:
        verdict.fail(f"FV↔WZ total mismatch: Δ={delta:+d}")

    return grand_fv, grand_wz, month_invoices, month_wz


# ── §2 Type-level match ──────────────────────────────────────────────────────


def section_type_match(
    inv_by_wz: dict[int, dict],
    doc_actions: dict[int, list[dict]],
    verdict: Verdict,
) -> list[dict]:
    """§2 — Type-level match: plastic FV = plastic WZ, glass FV = glass WZ."""
    mismatches: list[dict] = []

    for wz_id, inv in sorted(inv_by_wz.items(), key=lambda x: sell_date_of(x[1])):
        fv_p, fv_g = invoice_bottles(inv)
        wz_p, wz_g = wz_bottles(wz_id, doc_actions)
        if (fv_p, fv_g) != (wz_p, wz_g) and (fv_p + fv_g > 0 or wz_p + wz_g > 0):
            mismatches.append(
                {
                    "number": inv.get("number", "?"),
                    "sell_date": sell_date_of(inv),
                    "fv_plastic": fv_p,
                    "fv_glass": fv_g,
                    "wz_plastic": wz_p,
                    "wz_glass": wz_g,
                }
            )

    if mismatches:
        for r in mismatches:
            dp = r["fv_plastic"] - r["wz_plastic"]
            dg = r["fv_glass"] - r["wz_glass"]
            print(
                f"  {r['number']:>18s}  sell={r['sell_date']}  "
                f"FV(P={r['fv_plastic']},G={r['fv_glass']}) "
                f"WZ(P={r['wz_plastic']},G={r['wz_glass']}) "
                f"ΔP={dp:+d} ΔG={dg:+d}"
            )
        verdict.fail(f"Type-level mismatch: {len(mismatches)} documents")
    else:
        print("  ✅ Typy plastik/szkło: FV = WZ")

    return mismatches


# ── §3 Orphan WZ ─────────────────────────────────────────────────────────────


def section_orphan_wz(
    wz_docs: list[dict],
    inv_by_wz: dict[int, dict],
    doc_actions: dict[int, list[dict]],
    verdict: Verdict,
) -> list[dict]:
    """§3 — WZ documents without a linked invoice."""
    orphans: list[dict] = []
    for wz in wz_docs:
        if wz["id"] not in inv_by_wz:
            p, g = wz_bottles(wz["id"], doc_actions)
            orphans.append({"wz": wz, "plastic": p, "glass": g, "total": p + g})

    if orphans:
        for o in orphans:
            wz = o["wz"]
            print(
                f"  WZ #{wz['id']}  nr={wz.get('number', '?'):>10s}  "
                f"issue={wz['issue_date']}  butelki={o['total']} "
                f"(P={o['plastic']}, G={o['glass']})"
            )
        total_btl = sum(o["total"] for o in orphans)
        print(f"\n  Łącznie orphan WZ: {len(orphans)}, butelki: {total_btl}")
        verdict.fail(f"Orphan WZ: {len(orphans)} documents, {total_btl} bottles")
    else:
        print("  ✅ Brak WZ bez faktur")

    return orphans


# ── §4 Invoices without WZ ───────────────────────────────────────────────────


def section_no_wz(
    invoices: list[dict],
    inv_by_wz: dict[int, dict],
    verdict: Verdict,
) -> list[dict]:
    """§4 — Invoices with bottle positions but no linked WZ."""
    wz_linked_ids = {inv["id"] for inv in inv_by_wz.values()}
    no_wz: list[dict] = []

    for inv in invoices:
        if inv["id"] in wz_linked_ids:
            continue
        p, g = invoice_bottles(inv)
        if p + g > 0:
            no_wz.append({"inv": inv, "plastic": p, "glass": g, "total": p + g})

    if no_wz:
        for item in no_wz:
            inv = item["inv"]
            print(
                f"  {doc_type_label(inv)} {inv.get('number', '?'):>18s}  "
                f"sell={sell_date_of(inv)}  butelki={item['total']} "
                f"(P={item['plastic']}, G={item['glass']})"
            )
        print(f"\n  Łącznie: {len(no_wz)}, butelki: {sum(i['total'] for i in no_wz)}")
        verdict.fail(f"Invoices without WZ: {len(no_wz)}")
    else:
        print("  ✅ Brak faktur bez WZ")

    return no_wz


# ── §5 Date comparison ───────────────────────────────────────────────────────


def section_date_comparison(
    inv_by_wz: dict[int, dict],
    wz_by_id: dict[int, dict],
    verdict: Verdict,
) -> list[dict]:
    """§5 — sell_date FV vs issue_date WZ (cross-month pairs)."""
    month_mismatch: list[dict] = []
    total_pairs = 0

    for wz_id, inv in inv_by_wz.items():
        wz = wz_by_id[wz_id]
        sell = sell_date_of(inv)
        wz_date = wz["issue_date"]
        sell_m, wz_m = month_of(sell), month_of(wz_date)
        p, g = invoice_bottles(inv)

        try:
            diff_days = (Date.fromisoformat(sell) - Date.fromisoformat(wz_date)).days
        except Exception:
            diff_days = None

        total_pairs += 1
        if sell_m != wz_m:
            month_mismatch.append(
                {
                    "inv_number": inv.get("number", "?"),
                    "sell_date": sell,
                    "wz_date": wz_date,
                    "sell_month": sell_m,
                    "wz_month": wz_m,
                    "diff_days": diff_days,
                    "bottles": p + g,
                }
            )

    if month_mismatch:
        print(f"  ❌ Daty FV↔WZ: {len(month_mismatch)}/{total_pairs} par niezgodnych")
        for r in sorted(month_mismatch, key=lambda x: x["sell_date"]):
            print(
                f"    {r['inv_number']:>18s}  sell={r['sell_date']}  wz={r['wz_date']}  "
                f"FV:{MONTHS_PL.get(r['sell_month'], '?')} "
                f"WZ:{MONTHS_PL.get(r['wz_month'], '?')}  btl={r['bottles']}"
            )
        verdict.fail(f"Cross-month FV↔WZ: {len(month_mismatch)} pairs")
    else:
        print(f"  ✅ Daty FV↔WZ: {total_pairs} par, zgodne")

    return month_mismatch


# ── §6 Cross-month sell/issue ─────────────────────────────────────────────────


def section_cross_month_sell_issue(
    invoices: list[dict],
    verdict: Verdict,
) -> list[dict]:
    """§6 — Invoices where sell_date and issue_date fall in different months."""
    mismatched: list[dict] = []

    for inv in invoices:
        sell = inv.get("sell_date", "")
        issue = inv.get("issue_date", "")
        if not sell or not issue:
            continue
        p, g = invoice_bottles(inv)
        if p + g == 0:
            continue
        if month_of(sell) != month_of(issue):
            mismatched.append(inv)

    if mismatched:
        print(f"  ⚠️  sell/issue cross-month: {len(mismatched)} faktur")
        for inv in sorted(mismatched, key=lambda x: x.get("sell_date", "")):
            p, g = invoice_bottles(inv)
            print(
                f"    {doc_type_label(inv)} {inv.get('number', '?'):>18s}  "
                f"sell={inv.get('sell_date', '')}  issue={inv.get('issue_date', '')}  "
                f"btl={p + g}"
            )
    else:
        print("  ✅ sell/issue_date w tym samym miesiącu")

    return mismatched


# ── §7 Numbering continuity ──────────────────────────────────────────────────


@dataclass
class SeriesResult:
    """Numbering analysis for a single invoice series."""

    series: str
    count: int
    first: int
    last: int
    expected: int
    gaps: list[int]
    duplicates: list[int]

    @property
    def ok(self) -> bool:
        return not self.gaps and not self.duplicates


def check_numbering(invoices: list[dict]) -> list[SeriesResult]:
    """Analyse invoice numbering continuity — pure logic, no I/O."""
    series_nums: dict[str, list[tuple[int, str]]] = defaultdict(list)

    for inv in invoices:
        nr = inv.get("number", "")
        parts = nr.split("/", 1)
        if len(parts) == 2:
            try:
                seq = int(parts[0])
                series = parts[1]
                series_nums[series].append((seq, nr))
            except ValueError:
                pass

    results: list[SeriesResult] = []
    for series in sorted(series_nums.keys()):
        nums = sorted(series_nums[series], key=lambda x: x[0])
        seqs = [n[0] for n in nums]

        seen: dict[int, int] = {}
        duplicates: list[int] = []
        for s in seqs:
            if s in seen:
                duplicates.append(s)
            seen[s] = seen.get(s, 0) + 1

        gaps: list[int] = []
        if seqs:
            for expected in range(seqs[0], seqs[-1] + 1):
                if expected not in seen:
                    gaps.append(expected)

        expected_count = (seqs[-1] - seqs[0] + 1) if seqs else 0
        results.append(
            SeriesResult(
                series=series,
                count=len(seqs),
                first=seqs[0] if seqs else 0,
                last=seqs[-1] if seqs else 0,
                expected=expected_count,
                gaps=gaps,
                duplicates=duplicates,
            )
        )
    return results


def section_numbering(
    invoices: list[dict],
    verdict: Verdict,
) -> None:
    """§7 — Invoice numbering continuity: detect gaps & duplicates per series."""
    results = check_numbering(invoices)
    issues_found = False
    for sr in results:
        if not sr.ok:
            issues_found = True
            print(
                f"\n  Seria /{sr.series}:  zakres {sr.first}–{sr.last}, "
                f"{sr.count} dokumentów (oczekiwano {sr.expected})"
            )
            if sr.duplicates:
                print(f"    ❌ Duplikaty: {sr.duplicates}")
            if sr.gaps:
                print(f"    ❌ Luki: {sr.gaps}")
        else:
            print(f"  Seria /{sr.series}:  {sr.first}–{sr.last}  ({sr.count} dok.) ✅")

    if issues_found:
        verdict.fail("Numbering issues found (gaps or duplicates)")


# ── §8 Stock balance ─────────────────────────────────────────────────────────


def section_stock_balance(
    all_actions: list[dict],
    products: list[dict],
    year: int,
    month: int | None,
    verdict: Verdict,
) -> None:
    """Stock balance: PZ/WZ movements for the audited period."""
    if month:
        prefix = f"{year}-{month:02d}"
    else:
        prefix = str(year)

    # ── Monthly movements for bottle products ─────────────────────────────
    month_pz: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    month_wz: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))

    for a in all_actions:
        issue = a.get("wd_issue_date", "")
        if not issue.startswith(prefix):
            continue
        name = BOTTLE_ALIASES.get(a["product_name"], a["product_name"])
        if name not in BOTTLE_PRODUCTS:
            continue
        m = int(issue.split("-")[1])
        qty = float(a["quantity"])
        if qty > 0:
            month_pz[m][name] += qty
        else:
            month_wz[m][name] += abs(qty)

    all_months = sorted(set(list(month_pz.keys()) + list(month_wz.keys())))
    totals_pz: dict[str, float] = defaultdict(float)
    totals_wz: dict[str, float] = defaultdict(float)

    for m in all_months:
        label = MONTHS_PL.get(m, "?")
        parts: list[str] = []
        for prod in sorted(BOTTLE_PRODUCTS):
            short = "P" if "plastik" in prod else "S"
            pz = month_pz[m].get(prod, 0)
            wz = month_wz[m].get(prod, 0)
            totals_pz[prod] += pz
            totals_wz[prod] += wz
            if pz > 0 or wz > 0:
                parts.append(f"{short}: PZ=+{pz:.0f} WZ=-{wz:.0f}")
        if parts:
            print(f"  {label:3s}  {'  '.join(parts)}")

    # ── Year totals ───────────────────────────────────────────────────────
    print(f"  {'─' * 50}")
    for prod in sorted(BOTTLE_PRODUCTS):
        short = "plastik" if "plastik" in prod else "szkło"
        pz = totals_pz.get(prod, 0)
        wz = totals_wz.get(prod, 0)
        net = pz - wz
        print(f"  {short:8s}  PZ=+{pz:.0f}  WZ=-{wz:.0f}  netto={net:+.0f}")

    # ── Current stock (informational) ─────────────────────────────────────
    for p in products:
        name = p.get("name", "")
        if name in BOTTLE_PRODUCTS:
            wq = float(p.get("warehouse_quantity") or 0)
            short = "plastik" if "plastik" in name else "szkło"
            print(f"  {short:8s}  stan magazynu: {wq:.0f}")


# ── §9 Anomalies ─────────────────────────────────────────────────────────────


def section_anomalies(
    inv_by_wz: dict[int, dict],
    wz_by_id: dict[int, dict],
    invoices: list[dict],
) -> None:
    """§4 — Large invoices, duplicate WZ links."""
    big = []
    for _wz_id, inv in inv_by_wz.items():
        p, g = invoice_bottles(inv)
        total = p + g
        if total > 72:
            big.append((inv, total))
    big.sort(key=lambda x: -x[1])

    wz_inv_count: dict[int, list] = defaultdict(list)
    for inv in invoices:
        wd_id = inv.get("warehouse_document_id")
        if wd_id and wd_id in wz_by_id:
            wz_inv_count[wd_id].append(inv)
    multi = {k: v for k, v in wz_inv_count.items() if len(v) > 1}

    if not big and not multi:
        print("  ✅ Brak anomalii")
        return

    if big:
        print("  >72 butelek:")
        for inv, total in big:
            print(
                f"    {doc_type_label(inv)} {inv.get('number', '?'):>18s}  "
                f"sell={sell_date_of(inv)}  btl={total}"
            )
    if multi:
        print("  Wiele faktur → ten sam WZ:")
        for wz_id, invs in multi.items():
            wz = wz_by_id[wz_id]
            print(f"    WZ #{wz_id} (nr={wz.get('number', '?')}) → {len(invs)} faktur")
