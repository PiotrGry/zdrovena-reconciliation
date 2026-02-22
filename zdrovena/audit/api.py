"""
Fakturownia API helpers – fetch invoices, WZ documents, warehouse actions, products.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from zdrovena.common import FakturowniaClient


def get_client() -> FakturowniaClient:
    """Return a :class:`FakturowniaClient` authenticated via macOS Keychain."""
    return FakturowniaClient.from_keyring()


# ── Date helpers ──────────────────────────────────────────────────────────────

def date_range(year: int, month: int | None = None, day: int | None = None) -> tuple[str, str]:
    """
    Return ``(date_from, date_to)`` ISO strings for the requested period.

    * ``year`` only → full year
    * ``year + month`` → that calendar month
    * ``year + month + day`` → single day
    """
    if day and month:
        d = date(year, month, day).isoformat()
        return d, d

    if month:
        d_from = date(year, month, 1)
        if month == 12:
            d_to = date(year + 1, 1, 1) - timedelta(days=1)
        else:
            d_to = date(year, month + 1, 1) - timedelta(days=1)
        return d_from.isoformat(), d_to.isoformat()

    return date(year, 1, 1).isoformat(), date(year, 12, 31).isoformat()


def month_of(date_str: str) -> int:
    """Extract month number from an ISO date string."""
    return int(date_str.split("-")[1]) if date_str else 0


def sell_date_of(inv: dict) -> str:
    """Return the effective sell date (falls back to issue_date)."""
    return inv.get("sell_date") or inv.get("issue_date", "")


def is_receipt(inv: dict) -> bool:
    """Return ``True`` if the invoice is a receipt (paragon)."""
    return inv.get("kind") == "receipt"


def doc_type_label(inv: dict) -> str:
    """Return ``'PAR'`` for receipts, ``'FV'`` for regular invoices."""
    return "PAR" if is_receipt(inv) else "FV"


def inv_sort_key(inv: dict) -> tuple[int, str]:
    """
    Sort key for invoices — by leading number in ``'12/02/2025'``-style numbers.

    Falls back to lexicographic order when the number doesn't start with digits.
    """
    nr = inv.get("number", "")
    parts = nr.split("/")
    try:
        return (int(parts[0]), nr)
    except (ValueError, IndexError):
        return (999_999, nr)


# ── Paginated fetch ──────────────────────────────────────────────────────────

def _paginate(
    client: FakturowniaClient,
    endpoint: str,
    params: dict[str, Any] | None = None,
    *,
    per_page: int = 100,
) -> list[dict]:
    """
    Generic paginated GET – keep fetching pages until the API returns
    an empty batch.
    """
    all_items: list[dict] = []
    page = 1
    base_params = dict(params or {})

    while True:
        base_params.update({"page": page, "per_page": per_page})
        batch = client.get_json(endpoint, params=base_params)
        if not batch:
            break
        all_items.extend(batch)
        if len(batch) < per_page:
            break
        page += 1

    return all_items


# ── Fetch helpers ─────────────────────────────────────────────────────────────

def fetch_invoices(
    client: FakturowniaClient,
    year: int,
    month: int | None = None,
    day: int | None = None,
    *,
    include_proforma: bool = False,
    by_sell_date: bool = True,
) -> list[dict]:
    """
    Fetch sales invoices.  Proformas are excluded unless *include_proforma* is set.

    When *by_sell_date* is True (default), the API query range is widened so
    that invoices whose ``sell_date`` falls in the requested period are captured
    even when ``issue_date`` differs.  The result is then filtered client-side.
    """
    d_from, d_to = date_range(year, month, day)

    if by_sell_date and (month or day):
        # Widen query: fetch ±2 months so we catch cross-month sell_dates
        first = date.fromisoformat(d_from).replace(day=1)
        q_from = (first - timedelta(days=61)).replace(day=1).isoformat()
        last = date.fromisoformat(d_to)
        q_to = (last + timedelta(days=62)).isoformat()
        invoices = client.fetch_invoices(q_from, q_to, income="yes", label="sales invoices")
        # Filter by sell_date within requested range
        invoices = [
            i for i in invoices
            if d_from <= sell_date_of(i) <= d_to
        ]
    else:
        invoices = client.fetch_invoices(d_from, d_to, income="yes", label="sales invoices")

    if not include_proforma:
        invoices = [i for i in invoices if i.get("kind") != "proforma"]
    return invoices


def fetch_wz_documents(client: FakturowniaClient, year: int, month: int | None = None) -> list[dict]:
    """Fetch WZ (warehouse issue) documents for *year* (optionally filtered to *month*)."""
    prefix = f"{year}-{month:02d}" if month else str(year)
    docs = _paginate(client, "warehouse_documents.json", {"kind": "wz"})
    return [d for d in docs if d["issue_date"].startswith(prefix)]


def fetch_warehouse_actions(client: FakturowniaClient) -> list[dict]:
    """Fetch all WZ warehouse actions (paginated)."""
    return _paginate(client, "warehouse_actions.json", {"kind": "wz"})


def fetch_all_warehouse_actions(client: FakturowniaClient) -> list[dict]:
    """Fetch ALL warehouse actions — PZ and WZ (paginated)."""
    return _paginate(client, "warehouse_actions.json")


def fetch_products(client: FakturowniaClient) -> list[dict]:
    """Fetch all products (paginated)."""
    return _paginate(client, "products.json")


# ── Lookup builders ───────────────────────────────────────────────────────────

def build_actions_by_doc(actions: list[dict]) -> dict[int, list[dict]]:
    """Group warehouse actions by ``warehouse_document_id``."""
    by_doc: dict[int, list[dict]] = defaultdict(list)
    for a in actions:
        by_doc[a["warehouse_document_id"]].append(a)
    return dict(by_doc)


def build_wz_by_id(wz_docs: list[dict]) -> dict[int, dict]:
    """Map ``id → wz_doc``."""
    return {w["id"]: w for w in wz_docs}


def build_inv_by_wz(invoices: list[dict], wz_by_id: dict[int, dict]) -> dict[int, dict]:
    """Map ``wz_id → invoice`` for invoices linked to a known WZ."""
    mapping: dict[int, dict] = {}
    for inv in invoices:
        wd_id = inv.get("warehouse_document_id")
        if wd_id and wd_id in wz_by_id:
            mapping[wd_id] = inv
    return mapping
