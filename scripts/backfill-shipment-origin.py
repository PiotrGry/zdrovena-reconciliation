#!/usr/bin/env python3
"""Backfill shipment_origin on drafts created before the field existed.

A draft holding a courier_draft_id was shipped by this system. A draft with a
tracking number but no courier_draft_id was dispatched by hand in a carrier
portal and the Shopify sync wrote the number back. Drafts with neither are left
alone — they are simply not shipped yet, and guessing would be worse than a
missing value.

Usage:
    python3 scripts/backfill-shipment-origin.py            # dry run
    python3 scripts/backfill-shipment-origin.py --apply    # write

Run --apply only after the code that writes this field is deployed, so the
backfill and the application agree on what the values mean.
"""

from __future__ import annotations

import argparse
import collections
import os
import sys


def classify(entity: dict) -> str | None:
    """Return the origin for a draft, or None when it cannot be known."""
    if str(entity.get("courier_draft_id") or "").strip():
        return "system"
    if str(entity.get("tracking_number") or "").strip():
        return "external"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write changes to storage")
    args = parser.parse_args()

    if not args.apply:
        print("DRY RUN — nothing will be written. Pass --apply to commit changes.\n")

    account_url = os.environ.get("AZURE_STORAGE_ACCOUNT_URL")
    if not account_url:
        print("AZURE_STORAGE_ACCOUNT_URL is not set", file=sys.stderr)
        return 1

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from zdrovena.common.shipping_store import ShippingStore

    store = ShippingStore(account_url=account_url)
    client = store._table_client()

    counts: collections.Counter[str] = collections.Counter()
    for entity in client.query_entities("PartitionKey eq 'drafts'"):
        if str(entity.get("shipment_origin") or "").strip():
            counts["already set — skipped"] += 1
            continue
        origin = classify(dict(entity))
        if origin is None:
            counts["no tracking yet — skipped"] += 1
            continue
        counts[origin] += 1
        if args.apply:
            store.update_draft(entity["RowKey"], {"shipment_origin": origin})

    print("Result:")
    for key, value in counts.most_common():
        print(f"  {key}: {value}")
    if not args.apply:
        print("\nRe-run with --apply to write these values.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
