"""The warehouse side of month-close, as issues the operator can see.

A month could be closed and mailed to the accountant with an invoice that has no
WZ behind it, or a WZ nothing was ever billed for. The audit that finds those
already existed -- in ``zdrovena audit``, a separate CLI somebody has to remember
to run. Nothing in the close workflow asked (issue #308).

Severity is configurable and defaults to ``warning``. The issue suggests
``blocker`` for both checks, but conditions that on first counting how many
recent months already fail -- a count that needs live Fakturownia data. Shipping
``blocker`` without it could wall off every close on day one, so the stricter
setting is one environment variable away instead of the default.

Only the two checks with a real failure condition are wired up. The stock-balance
section prints movements and current stock but never fails; there is no rule in
it to map, and inventing a threshold is the owner's decision.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from zdrovena.audit.api import (
    build_actions_by_doc,
    build_inv_by_wz,
    build_wz_by_id,
    fetch_invoices,
    fetch_warehouse_actions,
    fetch_wz_documents,
)
from zdrovena.audit.warehouse_checks import find_invoices_without_wz, find_orphan_wz

logger = logging.getLogger("zdrovena.month_closing.warehouse_audit")

DEFAULT_SEVERITY = "warning"
_ALLOWED_SEVERITIES = ("info", "warning", "blocker")
_SEVERITY_ENV = "MONTH_CLOSE_WAREHOUSE_SEVERITY"

#: How many document numbers to name before switching to a count.
_MAX_LISTED = 5


def resolve_severity() -> str:
    configured = os.environ.get(_SEVERITY_ENV, "").strip().lower()
    if configured in _ALLOWED_SEVERITIES:
        return configured
    if configured:
        logger.warning(
            "%s=%s is not one of %s — using %s.",
            _SEVERITY_ENV,
            configured,
            _ALLOWED_SEVERITIES,
            DEFAULT_SEVERITY,
        )
    return DEFAULT_SEVERITY


def _issue(issue_id: str, severity: str, message: str) -> dict[str, Any]:
    return {"id": issue_id, "severity": severity, "message": message, "stage": "check"}


def _summarise(numbers: list[str]) -> str:
    listed = ", ".join(numbers[:_MAX_LISTED])
    if len(numbers) > _MAX_LISTED:
        return f"{listed} i {len(numbers) - _MAX_LISTED} więcej"
    return listed


def warehouse_issues(client: Any, year: int, month: int) -> list[dict[str, Any]]:
    """Run the warehouse consistency checks for one month.

    A provider failure is reported as its own issue rather than swallowed:
    returning an empty list would tell the operator the warehouse is clean when
    it was never actually checked.
    """
    try:
        invoices = fetch_invoices(client, year, month=month)
        wz_docs = fetch_wz_documents(client, year, month=month)
        wz_actions = fetch_warehouse_actions(client)
    except Exception as exc:
        logger.warning("Warehouse audit could not fetch its inputs: %s", exc)
        return [
            _issue(
                "warehouse-audit-unavailable",
                "warning",
                f"Nie udało się sprawdzić strony magazynowej: {exc}",
            )
        ]

    wz_by_id = build_wz_by_id(wz_docs)
    doc_actions = build_actions_by_doc(wz_actions)
    inv_by_wz = build_inv_by_wz(invoices, wz_by_id)

    severity = resolve_severity()
    issues: list[dict[str, Any]] = []

    missing_wz = find_invoices_without_wz(invoices, inv_by_wz)
    if missing_wz:
        numbers = [str(item["inv"].get("number", "?")) for item in missing_wz]
        issues.append(
            _issue(
                "warehouse-invoices-without-wz",
                severity,
                f"Faktury z pozycjami butelkowymi bez WZ ({len(missing_wz)}): "
                f"{_summarise(numbers)}.",
            )
        )

    orphans = find_orphan_wz(wz_docs, inv_by_wz, doc_actions)
    if orphans:
        numbers = [str(item["wz"].get("number", "?")) for item in orphans]
        bottles = sum(int(item["total"]) for item in orphans)
        issues.append(
            _issue(
                "warehouse-orphan-wz",
                severity,
                f"WZ bez faktury ({len(orphans)}, butelek: {bottles}): {_summarise(numbers)} — "
                "możliwa niezafakturowana sprzedaż.",
            )
        )

    return issues
