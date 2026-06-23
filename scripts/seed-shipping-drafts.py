#!/usr/bin/env python3
"""Seed local / Azurite Table Storage with test shipping drafts.

Usage:
    python3 scripts/seed-shipping-drafts.py           # dodaje drafty, nie nadpisuje istniejących
    python3 scripts/seed-shipping-drafts.py --clear   # czyści i wypełnia od nowa
    python3 scripts/seed-shipping-drafts.py --status  # tylko pokazuje co jest w storage

Env (opcjonalnie):
    AZURE_STORAGE_CONNECTION_STRING  → Azurite / Azure Table Storage
    Domyślnie: lokalny JSON w ~/.zdrovena/storage/
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Pozwól importować zdrovena bez instalacji pakietu
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zdrovena.common.shipping_store import ShippingStore


_AZURITE_CONN = (
    "DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;"
    "AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;"
    "BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1;"
    "TableEndpoint=http://127.0.0.1:10002/devstoreaccount1;"
)


def _azure_importable() -> bool:
    try:
        import azure.data.tables  # noqa: F401
        return True
    except ImportError:
        return False


def _azurite_port_open() -> bool:
    import socket
    try:
        socket.create_connection(("127.0.0.1", 10002), timeout=1).close()
        return True
    except OSError:
        return False


def make_store() -> ShippingStore:
    conn = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
    if conn:
        return ShippingStore(connection_string=conn)
    if _azurite_port_open():
        if not _azure_importable():
            print(
                "\n❌  Azurite działa (port 10002) ale brak pakietu azure-data-tables.\n"
                "   Uruchom seeder wewnątrz kontenera:\n\n"
                "     docker compose exec api python3 /app/scripts/seed-shipping-drafts.py --clear-all\n"
            )
            sys.exit(1)
        print("  ℹ  Azurite wykryty — używam Table Storage (port 10002)")
        return ShippingStore(connection_string=_AZURITE_CONN)
    return ShippingStore()


_SEED_IDS = [
    "seed-draft-1110-aaaa",
    "seed-draft-1111-bbbb",
    "seed-draft-1112-cccc",
    "seed-draft-1113-dddd",
    "seed-draft-1114-eeee",
]


def _draft(
    draft_id: str,
    order_number: str,
    customer_name: str,
    courier: str,
    service: str,
    status: str,
    packages_count: int,
    total_qty: int,
    order_items: list,
    receiver: dict,
    shipping_address: dict,
    tracking_number: str | None = None,
    courier_draft_id: str | None = None,
    pickup_ordered: bool = False,
    error: str | None = None,
) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "id": draft_id,
        "created_at": now,
        "source": "shopify",
        "shopify_order_id": f"50000000{order_number}",
        "shopify_order_number": order_number,
        "customer_name": customer_name,
        "courier": courier,
        "service": service,
        "status": status,
        "tracking_number": tracking_number,
        "courier_draft_id": courier_draft_id,
        "packages_count": packages_count,
        "total_qty": total_qty,
        "order_items": order_items,
        "pickup_ordered": pickup_ordered,
        "receiver": receiver,
        "shipping_address": shipping_address,
        "parcel": {"template": "small", "weight_kg": None},
        "error": error,
    }


TEST_DRAFTS = [
    # 3 zgrzewki × 12 butelek = 36 butelek → 1 karton (3 zgrzewki/karton)
    _draft(
        draft_id=_SEED_IDS[0],
        order_number="1110",
        customer_name="Marek Zielinski",
        courier="inpost",
        service="inpost_courier_standard",
        status="pending",
        packages_count=1,
        total_qty=3,
        order_items=[{"name": "HUMIO - woda alkaliczna, 12 butelek", "quantity": 3}],
        receiver={"first_name": "Marek", "last_name": "Zielinski",
                  "email": "marek@example.com", "phone": "+48601234567", "locker_id": ""},
        shipping_address={"street": "ul. Wierzbowa 12", "city": "Wroclaw", "post_code": "50-001"},
    ),
    # 1 zgrzewka × 12 butelek w szkle → 1 karton
    _draft(
        draft_id=_SEED_IDS[1],
        order_number="1111",
        customer_name="Katarzyna Nowak",
        courier="inpost",
        service="inpost_locker_standard",
        status="pending",
        packages_count=1,
        total_qty=1,
        order_items=[{"name": "HUMIO - woda alkaliczna, 12 butelek w szkle", "quantity": 1}],
        receiver={"first_name": "Katarzyna", "last_name": "Nowak",
                  "email": "katarzyna@example.com", "phone": "+48602345678", "locker_id": "KRK01M"},
        shipping_address={"street": "", "city": "Krakow", "post_code": "31-001"},
    ),
    # 6 zgrzewek × 12 butelek = 72 butelki → 2 kartony
    _draft(
        draft_id=_SEED_IDS[2],
        order_number="1112",
        customer_name="Tomasz Wisniewski",
        courier="apaczka",
        service="apaczka",
        status="pending",
        packages_count=2,
        total_qty=6,
        order_items=[{"name": "HUMIO - woda alkaliczna, 12 butelek", "quantity": 6}],
        receiver={"first_name": "Tomasz", "last_name": "Wisniewski",
                  "email": "tomasz@example.com", "phone": "+48603456789", "locker_id": ""},
        shipping_address={"street": "ul. Lipowa 5", "city": "Gdansk", "post_code": "80-001"},
    ),
    # 2 zgrzewki → 1 karton (niepełny: 2 zgrzewki w kartonie)
    _draft(
        draft_id=_SEED_IDS[3],
        order_number="1113",
        customer_name="Anna Kowalska",
        courier="inpost",
        service="inpost_courier_standard",
        status="created",
        packages_count=1,
        total_qty=2,
        order_items=[{"name": "HUMIO - woda alkaliczna, 12 butelek w szkle", "quantity": 2}],
        receiver={"first_name": "Anna", "last_name": "Kowalska",
                  "email": "anna@example.com", "phone": "+48604567890", "locker_id": ""},
        shipping_address={"street": "ul. Kwiatowa 3", "city": "Warszawa", "post_code": "00-001"},
        tracking_number="630001234567890201",
        courier_draft_id="inpost-draft-bb001",
        pickup_ordered=True,
    ),
    # 3 zgrzewki × 12 butelek w szkle → 1 karton, błąd API
    _draft(
        draft_id=_SEED_IDS[4],
        order_number="1114",
        customer_name="Beata Wojcik",
        courier="inpost",
        service="inpost_locker_standard",
        status="error",
        packages_count=1,
        total_qty=3,
        order_items=[{"name": "HUMIO - woda alkaliczna, 12 butelek w szkle", "quantity": 3}],
        receiver={"first_name": "Beata", "last_name": "Wojcik",
                  "email": "beata@example.com", "phone": "+48605678901", "locker_id": "WAW99B"},
        shipping_address={"street": "", "city": "Warszawa", "post_code": "02-001"},
        error="InPost API 401: invalid token — check inpost_api_token in Key Vault",
    ),
]


def seed(store: ShippingStore, clear: bool) -> None:
    if clear:
        for seed_id in _SEED_IDS:
            store.delete_draft(seed_id)
        print(f"  ✗ usunięto {len(_SEED_IDS)} draftów testowych")

    for draft in TEST_DRAFTS:
        store.upsert_draft(draft)
        print(f"  ↑ #{draft['shopify_order_number']} {draft['customer_name']:<22s} [{draft['status']}]")

    print(f"\n✅ {len(TEST_DRAFTS)} draftów testowych nadpisanych")


def show_status(store: ShippingStore) -> None:
    drafts = store.list_drafts()
    if not drafts:
        print("  (brak draftów)")
        return
    for d in drafts:
        print(f"  #{d['shopify_order_number']:6s}  {d['customer_name']:<22s}  {d['status']:<8s}  {d['courier']}")
    print(f"\n  Łącznie: {len(drafts)}")


def clear_all(store: ShippingStore) -> None:
    drafts = store.list_drafts(limit=1000)
    for d in drafts:
        store.delete_draft(d["id"])
    print(f"  ✗ usunięto {len(drafts)} draftów")


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed test shipping drafts")
    parser.add_argument("--clear", action="store_true", help="Usuń testowe drafty i dodaj od nowa")
    parser.add_argument("--clear-all", action="store_true", help="Wyczyść WSZYSTKIE drafty i zacznij od nowa")
    parser.add_argument("--status", action="store_true", help="Pokaż aktualny stan storage")
    args = parser.parse_args()

    store = make_store()

    if args.status:
        print("Stan shipping drafts:")
        show_status(store)
        return

    if args.clear_all:
        print("Czyszczę wszystkie drafty...")
        clear_all(store)
        print("Seedowanie shipping drafts:")
        seed(store, clear=False)
        return

    print("Seedowanie shipping drafts:")
    seed(store, clear=args.clear)


if __name__ == "__main__":
    main()
