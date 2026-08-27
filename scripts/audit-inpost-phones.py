#!/usr/bin/env python3
"""Report InPost drafts that will stop shipping when the carrier enforces phones.

InPost makes the recipient phone mandatory and validated on 2026-09-08. A draft
whose stored phone will not normalise cannot be shipped after that date. This
script finds those drafts so they can be fixed in advance, instead of surfacing
one failed shipment at a time on the morning of the deadline.

Read-only. It never writes to the store, and it never invents a phone number.

Usage:
    AZURE_STORAGE_ACCOUNT_URL=https://<account>.blob.core.windows.net \
        python3 scripts/audit-inpost-phones.py
"""

from __future__ import annotations

import os
import sys

# Statuses past which no further ShipX POST happens for this draft, so a bad
# phone on one of them can no longer break anything.
TERMINAL_STATUSES = frozenset({"created", "cancelled"})


def needs_attention(draft: dict) -> bool:
    """Return whether this draft would fail InPost's phone validation."""
    from zdrovena.common.shipping_format import normalize_pl_phone

    if draft.get("courier") != "inpost":
        return False
    if draft.get("status") in TERMINAL_STATUSES:
        return False
    return not normalize_pl_phone((draft.get("receiver") or {}).get("phone"))


def main() -> int:
    account_url = os.environ.get("AZURE_STORAGE_ACCOUNT_URL")
    if not account_url:
        print("AZURE_STORAGE_ACCOUNT_URL is not set", file=sys.stderr)
        return 1

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from zdrovena.common.shipping_store import ShippingStore, _deserialize

    store = ShippingStore(account_url=account_url)
    client = store._table_client()

    scanned = 0
    affected: list[dict] = []
    # Streamed rather than list_drafts(limit=...): no need to hold every draft in
    # memory, and it keeps this off the full-partition-scan pile in issue #316.
    for entity in client.query_entities("PartitionKey eq 'drafts'"):
        scanned += 1
        draft = _deserialize(dict(entity))
        if needs_attention(draft):
            affected.append(draft)

    print(f"Scanned {scanned} drafts; {len(affected)} would fail InPost phone validation.")
    if not affected:
        return 0

    print()
    print(f"{'draft id':38} {'order':10} {'status':18} stored phone")
    for draft in sorted(affected, key=lambda d: str(d.get("shopify_order_number") or "")):
        raw = (draft.get("receiver") or {}).get("phone")
        draft_id = str(draft.get("id") or "")
        order_number = str(draft.get("shopify_order_number") or "")
        status = str(draft.get("status") or "")
        print(f"{draft_id:38} {order_number:10} {status:18} {raw!r}")
    print()
    print('Fix each one in the portal ("Telefon odbiorcy") or in the source order.')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
