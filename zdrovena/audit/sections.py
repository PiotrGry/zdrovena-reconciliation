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
2. Type-level match (plastic/glass per document)
3. Orphan WZ (no linked invoice)
4. Invoices without WZ
5. Date comparison (sell_date vs WZ issue_date)
6. Cross-month sell/issue on same invoice
7. Numbering continuity (gaps & duplicates)
8. Stock balance (ΣPZ − ΣWZ = warehouse_quantity)
9. Anomalies (large invoices, multi-linked WZ)
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date as Date
from typing import TYPE_CHECKING

from zdrovena.audit.api import (
    doc_type_label,
    month_of,
    sell_date_of,
)
from zdrovena.audit.bottles import BOTTLE_PRODUCTS, invoice_bottles, wz_bottles
from zdrovena.common.formatting import MONTHS_PL

if TYPE_CHECKING:
    from zdrovena.audit.commands.audit_cmd import Verdict


# ── §1 Recount ────────────────────────────────────────────────────────────────

def section_recount(
    inv_by_wz: dict[int, dict],
    doc_actions: dict[int, list[dict]],
    verdict: Verdict,
) -> tuple[int, int, dict[int, list[dict]], dict[int, int]]:
    """§1 — Recount every invoice and WZ by month (sell_date)."""
    month_invoices: dict[int, list[dict]] = defaultdict(list)
    month_wz: dict[int, int] = defaultdict(int)

    for wz_id, inv in sorted(inv_by_wz.items(), key=lambda x: sell_date_of(x[1])):
        sell = sell_date_of(inv)
        m = month_of(sell)
        p, g = invoice_bottles(inv)
        total = p + g
        if total > 0:
            month_invoices[m].append({
                "number": inv.get("number", "?"),
                "kind": inv.get("kind", "?"),
                "sell_date": sell,
                "issue_date": inv.get("issue_date", ""),
                "plastic": p, "glass": g, "total": total,
            })

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
        for i, item in enumerate(items, 1):
            kind_tag = "PAR" if item["kind"] == "receipt" else "VAT"
            print(f"    {i:3d}. {item['number']:>18s} [{kind_tag}]  "
                  f"sell={item['sell_date']}  issue={item['issue_date']}  "
                  f"plastik={item['plastic']:4d}  szkło={item['glass']:3d}  "
                  f"RAZEM={item['total']:4d}")

    print(f"\n  {'─' * 80}")
    delta = grand_fv - grand_wz
    print(f"  RAZEM ROK:  FV={grand_fv}  WZ={grand_wz}  Δ={delta:+d}")

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
            mismatches.append({
                "number": inv.get("number", "?"),
                "sell_date": sell_date_of(inv),
                "fv_plastic": fv_p, "fv_glass": fv_g,
                "wz_plastic": wz_p, "wz_glass": wz_g,
            })

    if mismatches:
        for r in mismatches:
            dp = r["fv_plastic"] - r["wz_plastic"]
            dg = r["fv_glass"] - r["wz_glass"]
            print(f"  {r['number']:>18s}  sell={r['sell_date']}  "
                  f"FV(P={r['fv_plastic']},G={r['fv_glass']}) "
                  f"WZ(P={r['wz_plastic']},G={r['wz_glass']}) "
                  f"ΔP={dp:+d} ΔG={dg:+d}")
        verdict.fail(f"Type-level mismatch: {len(mismatches)} documents")
    else:
        print("  ✅ Plastik i szkło zgadzają się na każdym dokumencie")

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
            print(f"  WZ #{wz['id']}  nr={wz.get('number', '?'):>10s}  "
                  f"issue={wz['issue_date']}  butelki={o['total']} "
                  f"(P={o['plastic']}, G={o['glass']})")
        total_btl = sum(o["total"] for o in orphans)
        print(f"\n  Łącznie orphan WZ: {len(orphans)}, butelki: {total_btl}")
        verdict.fail(f"Orphan WZ: {len(orphans)} documents, {total_btl} bottles")
    else:
        print("  Brak ✅")

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
            print(f"  {doc_type_label(inv)} {inv.get('number', '?'):>18s}  "
                  f"sell={sell_date_of(inv)}  butelki={item['total']} "
                  f"(P={item['plastic']}, G={item['glass']})")
        print(f"\n  Łącznie: {len(no_wz)}, butelki: {sum(i['total'] for i in no_wz)}")
        verdict.fail(f"Invoices without WZ: {len(no_wz)}")
    else:
        print("  Brak ✅")

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
            month_mismatch.append({
                "inv_number": inv.get("number", "?"),
                "sell_date": sell, "wz_date": wz_date,
                "sell_month": sell_m, "wz_month": wz_m,
                "diff_days": diff_days, "bottles": p + g,
            })

    print(f"\n  Łącznie par FV↔WZ: {total_pairs}")
    if month_mismatch:
        print(f"  ⚠️  Miesiące się różnią: {len(month_mismatch)}")
        for r in sorted(month_mismatch, key=lambda x: x["sell_date"]):
            print(f"    {r['inv_number']:>18s}  sell={r['sell_date']}  wz={r['wz_date']}  "
                  f"FV:{MONTHS_PL.get(r['sell_month'], '?')} "
                  f"WZ:{MONTHS_PL.get(r['wz_month'], '?')}  btl={r['bottles']}")
        verdict.fail(f"Cross-month FV↔WZ: {len(month_mismatch)} pairs")
    else:
        print("  ✅ Wszystkie miesiące się zgadzają")

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
        print(f"\n  ⚠️  Faktury cross-month: {len(mismatched)}")
        for inv in sorted(mismatched, key=lambda x: x.get("sell_date", "")):
            p, g = invoice_bottles(inv)
            print(f"    {doc_type_label(inv)} {inv.get('number', '?'):>18s}  "
                  f"sell={inv.get('sell_date', '')}  issue={inv.get('issue_date', '')}  "
                  f"btl={p + g}")
    else:
        print("  ✅ sell_date i issue_date zawsze w tym samym miesiącu")

    return mismatched


# ── §7 Numbering continuity ──────────────────────────────────────────────────

def section_numbering(
    invoices: list[dict],
    verdict: Verdict,
) -> None:
    """§7 — Invoice numbering continuity: detect gaps & duplicates per series."""
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

    issues_found = False
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

        if duplicates or gaps:
            issues_found = True
            print(f"\n  Seria /{series}:  zakres {seqs[0]}–{seqs[-1]}, "
                  f"{len(seqs)} dokumentów")
            if duplicates:
                print(f"    ❌ Duplikaty: {duplicates}")
            if gaps:
                print(f"    ❌ Luki: {gaps}")
        else:
            print(f"  Seria /{series}:  {seqs[0]}–{seqs[-1]}  ({len(seqs)} dok.) ✅")

    if issues_found:
        verdict.fail("Numbering issues found (gaps or duplicates)")


# ── §8 Stock balance ─────────────────────────────────────────────────────────

def section_stock_balance(
    all_actions: list[dict],
    products: list[dict],
    verdict: Verdict,
) -> None:
    """§8 — Stock balance: ΣPZ − ΣWZ = warehouse_quantity."""
    computed: dict[str, float] = defaultdict(float)
    for a in all_actions:
        computed[a["product_name"]] += float(a["quantity"])

    actual: dict[str, float] = {}
    for p in products:
        wq = float(p.get("warehouse_quantity") or 0)
        if wq != 0 or p.get("name", "") in BOTTLE_PRODUCTS:
            actual[p["name"]] = wq

    issues_found = False
    for name in sorted(set(list(computed.keys()) + list(actual.keys()))):
        if name not in BOTTLE_PRODUCTS and name not in actual:
            continue
        c = computed.get(name, 0)
        a = actual.get(name, 0)
        delta = c - a
        if abs(delta) > 0.5:
            issues_found = True
            print(f"  ❌ {name:45s}  computed={c:>+.0f}  actual={a:>+.0f}  Δ={delta:>+.0f}")
        else:
            print(f"  ✅ {name:45s}  stock={a:>.0f}")

    if issues_found:
        verdict.fail("Stock balance mismatch")


# ── §9 Anomalies ─────────────────────────────────────────────────────────────

def section_anomalies(
    inv_by_wz: dict[int, dict],
    wz_by_id: dict[int, dict],
    invoices: list[dict],
) -> None:
    """§9 — Large invoices, duplicate WZ links."""
    # a) Large invoices
    print("\n  a) Dokumenty z >72 butelkami:")
    big = []
    for wz_id, inv in inv_by_wz.items():
        p, g = invoice_bottles(inv)
        total = p + g
        if total > 72:
            big.append((inv, total))
    big.sort(key=lambda x: -x[1])
    if big:
        for inv, total in big:
            print(f"    {doc_type_label(inv)} {inv.get('number', '?'):>18s}  "
                  f"sell={sell_date_of(inv)}  btl={total}")
    else:
        print("    Brak")

    # b) Multiple invoices linked to the same WZ
    print("\n  b) Wiele faktur → ten sam WZ:")
    wz_inv_count: dict[int, list] = defaultdict(list)
    for inv in invoices:
        wd_id = inv.get("warehouse_document_id")
        if wd_id and wd_id in wz_by_id:
            wz_inv_count[wd_id].append(inv)
    multi = {k: v for k, v in wz_inv_count.items() if len(v) > 1}
    if multi:
        for wz_id, invs in multi.items():
            wz = wz_by_id[wz_id]
            print(f"    WZ #{wz_id} (nr={wz.get('number', '?')}) → {len(invs)} faktur")
    else:
        print("    Brak ✅")
