#!/usr/bin/env python3
"""Seed local / Azurite Table Storage with test shipping drafts.

Usage:
    python3 scripts/seed-shipping-drafts.py            # overwrite seed drafts
    python3 scripts/seed-shipping-drafts.py --clear    # delete seed drafts and re-seed
    python3 scripts/seed-shipping-drafts.py --clear-all  # wipe all drafts and re-seed
    python3 scripts/seed-shipping-drafts.py --status   # show current storage state

Env:
    AZURE_STORAGE_CONNECTION_STRING  -> Azurite / Azure Table Storage
    Default: local JSON at ~/.zdrovena/storage/
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
                "\n❌  Azurite is running (port 10002) but azure-data-tables is not installed.\n"
                "   Run the seeder inside the container:\n\n"
                "     docker compose exec api python3 /app/scripts/seed-shipping-drafts.py --clear-all\n"
            )
            sys.exit(1)
        print("  i  Azurite detected — using Table Storage (port 10002)")
        return ShippingStore(connection_string=_AZURITE_CONN)
    return ShippingStore()


_SEED_IDS = [
    "seed-draft-1110-aaaa",
    "seed-draft-1111-bbbb",
    "seed-draft-1112-cccc",
    "seed-draft-1113-dddd",
    "seed-draft-1114-eeee",
    "seed-draft-1115-ffff",
    "seed-draft-1116-gggg",
    "seed-draft-1117-hhhh",
    "seed-draft-1118-iiii",
    "seed-draft-1119-jjjj",
    "seed-draft-1120-kkkk",
    "seed-draft-1121-llll",
    "seed-draft-1122-mmmm",
    "seed-draft-1123-nnnn",
    "seed-draft-1124-oooo",
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


_P12 = "HUMIO - woda alkaliczna, 12 butelek"
_G12 = "HUMIO - woda alkaliczna, 12 butelek w szkle"

TEST_DRAFTS = [
    # 1110 — InPost Kurier, pending, plastik 1 zgrzewka → 1-pak
    _draft(_SEED_IDS[0], "1110", "Marek Zielinski",
           "inpost", "inpost_courier_standard", "pending",
           packages_count=1, total_qty=1,
           order_items=[{"name": _P12, "quantity": 1}],
           receiver={"first_name": "Marek", "last_name": "Zielinski",
                     "email": "marek@example.com", "phone": "+48601234567", "locker_id": ""},
           shipping_address={"street": "ul. Wierzbowa 12", "city": "Wroclaw", "post_code": "50-001"}),

    # 1111 — InPost Kurier, pending, plastik 3 zgrzewki → 1×3-pak
    _draft(_SEED_IDS[1], "1111", "Piotr Kowalczyk",
           "inpost", "inpost_courier_standard", "pending",
           packages_count=1, total_qty=3,
           order_items=[{"name": _P12, "quantity": 3}],
           receiver={"first_name": "Piotr", "last_name": "Kowalczyk",
                     "email": "piotr@example.com", "phone": "+48602345678", "locker_id": ""},
           shipping_address={"street": "ul. Lipowa 5", "city": "Gdansk", "post_code": "80-001"}),

    # 1112 — InPost Kurier, pending, plastik 5 zgrzewek → 1×3-pak + 1×2-pak
    _draft(_SEED_IDS[2], "1112", "Tomasz Wisniewski",
           "inpost", "inpost_courier_standard", "pending",
           packages_count=2, total_qty=5,
           order_items=[{"name": _P12, "quantity": 5}],
           receiver={"first_name": "Tomasz", "last_name": "Wisniewski",
                     "email": "tomasz@example.com", "phone": "+48603456789", "locker_id": ""},
           shipping_address={"street": "ul. Brzozowa 8", "city": "Krakow", "post_code": "31-100"}),

    # 1113 — InPost Kurier, pending, szkło 1 zgrzewka → 1×szkło
    _draft(_SEED_IDS[3], "1113", "Anna Kowalska",
           "inpost", "inpost_courier_standard", "pending",
           packages_count=1, total_qty=1,
           order_items=[{"name": _G12, "quantity": 1}],
           receiver={"first_name": "Anna", "last_name": "Kowalska",
                     "email": "anna@example.com", "phone": "+48604567890", "locker_id": ""},
           shipping_address={"street": "ul. Kwiatowa 3", "city": "Warszawa", "post_code": "00-001"}),

    # 1114 — InPost Kurier, pending, mixed 2×plastik + 1×szkło → 1×2-pak + 1×szkło
    _draft(_SEED_IDS[4], "1114", "Beata Wojcik",
           "inpost", "inpost_courier_standard", "pending",
           packages_count=2, total_qty=3,
           order_items=[{"name": _P12, "quantity": 2}, {"name": _G12, "quantity": 1}],
           receiver={"first_name": "Beata", "last_name": "Wojcik",
                     "email": "beata@example.com", "phone": "+48605678901", "locker_id": ""},
           shipping_address={"street": "ul. Prusa 3", "city": "Lublin", "post_code": "20-001"}),

    # 1115 — InPost Paczkomat, pending, plastik 3 zgrzewki → 1×3-pak
    _draft(_SEED_IDS[5], "1115", "Katarzyna Nowak",
           "inpost", "inpost_locker_standard", "pending",
           packages_count=1, total_qty=3,
           order_items=[{"name": _P12, "quantity": 3}],
           receiver={"first_name": "Katarzyna", "last_name": "Nowak",
                     "email": "katarzyna@example.com", "phone": "+48606789012", "locker_id": "KRK01M"},
           shipping_address={"street": "", "city": "Krakow", "post_code": "31-001"}),

    # 1116 — InPost Paczkomat, pending, szkło 2 zgrzewki → 2×szkło
    _draft(_SEED_IDS[6], "1116", "Michał Dąbrowski",
           "inpost", "inpost_locker_standard", "pending",
           packages_count=2, total_qty=2,
           order_items=[{"name": _G12, "quantity": 2}],
           receiver={"first_name": "Michał", "last_name": "Dąbrowski",
                     "email": "michal@example.com", "phone": "+48607890123", "locker_id": "WAW88C"},
           shipping_address={"street": "", "city": "Warszawa", "post_code": "02-001"}),

    # 1117 — InPost Paczkomat, pending, mixed 1×plastik + 2×szkło → 1×1-pak + 2×szkło
    _draft(_SEED_IDS[7], "1117", "Zofia Maj",
           "inpost", "inpost_locker_standard", "pending",
           packages_count=3, total_qty=3,
           order_items=[{"name": _P12, "quantity": 1}, {"name": _G12, "quantity": 2}],
           receiver={"first_name": "Zofia", "last_name": "Maj",
                     "email": "zofia@example.com", "phone": "+48608901234", "locker_id": "GDA05B"},
           shipping_address={"street": "", "city": "Gdansk", "post_code": "80-002"}),

    # 1118 — Apaczka, pending, plastik 6 zgrzewek → 2×3-pak
    _draft(_SEED_IDS[8], "1118", "Krzysztof Lewandowski",
           "apaczka", "apaczka", "pending",
           packages_count=2, total_qty=6,
           order_items=[{"name": _P12, "quantity": 6}],
           receiver={"first_name": "Krzysztof", "last_name": "Lewandowski",
                     "email": "krzysztof@example.com", "phone": "+48609012345", "locker_id": ""},
           shipping_address={"street": "ul. Słoneczna 7", "city": "Poznan", "post_code": "60-001"}),

    # 1119 — Apaczka, pending, szkło 3 zgrzewki → 3×szkło
    _draft(_SEED_IDS[9], "1119", "Aleksandra Wójcik",
           "apaczka", "apaczka", "pending",
           packages_count=3, total_qty=3,
           order_items=[{"name": _G12, "quantity": 3}],
           receiver={"first_name": "Aleksandra", "last_name": "Wójcik",
                     "email": "aleksandra@example.com", "phone": "+48610123456", "locker_id": ""},
           shipping_address={"street": "ul. Różana 2", "city": "Wroclaw", "post_code": "51-001"}),

    # 1120 — InPost Kurier, created, plastik 3 zgrzewki, pickup_ordered=false
    _draft(_SEED_IDS[10], "1120", "Paweł Kaminski",
           "inpost", "inpost_courier_standard", "created",
           packages_count=1, total_qty=3,
           order_items=[{"name": _P12, "quantity": 3}],
           receiver={"first_name": "Paweł", "last_name": "Kaminski",
                     "email": "pawel@example.com", "phone": "+48611234567", "locker_id": ""},
           shipping_address={"street": "ul. Jagiellońska 4", "city": "Warszawa", "post_code": "03-001"},
           tracking_number="630001234567890300", courier_draft_id="mock-inpost-1120",
           pickup_ordered=False),

    # 1121 — InPost Kurier, created, szkło 1 zgrzewka, pickup_ordered=true
    _draft(_SEED_IDS[11], "1121", "Monika Szymanska",
           "inpost", "inpost_courier_standard", "created",
           packages_count=1, total_qty=1,
           order_items=[{"name": _G12, "quantity": 1}],
           receiver={"first_name": "Monika", "last_name": "Szymanska",
                     "email": "monika@example.com", "phone": "+48612345678", "locker_id": ""},
           shipping_address={"street": "ul. Piękna 9", "city": "Krakow", "post_code": "31-200"},
           tracking_number="630001234567890301", courier_draft_id="mock-inpost-1121",
           pickup_ordered=True),

    # 1122 — InPost Paczkomat, created, mixed 3×plastik + 1×szkło, pickup_ordered=false
    _draft(_SEED_IDS[12], "1122", "Rafał Mazur",
           "inpost", "inpost_locker_standard", "created",
           packages_count=2, total_qty=4,
           order_items=[{"name": _P12, "quantity": 3}, {"name": _G12, "quantity": 1}],
           receiver={"first_name": "Rafał", "last_name": "Mazur",
                     "email": "rafal@example.com", "phone": "+48613456789", "locker_id": "LDZ02A"},
           shipping_address={"street": "", "city": "Lodz", "post_code": "90-001"},
           tracking_number="630001234567890302", courier_draft_id="mock-inpost-1122",
           pickup_ordered=False),

    # 1123 — Apaczka, created, plastik 4 zgrzewki → 1×3-pak + 1×1-pak
    _draft(_SEED_IDS[13], "1123", "Magdalena Kaczmarek",
           "apaczka", "apaczka", "created",
           packages_count=2, total_qty=4,
           order_items=[{"name": _P12, "quantity": 4}],
           receiver={"first_name": "Magdalena", "last_name": "Kaczmarek",
                     "email": "magdalena@example.com", "phone": "+48614567890", "locker_id": ""},
           shipping_address={"street": "ul. Mickiewicza 11", "city": "Szczecin", "post_code": "70-001"},
           tracking_number="APZ11230000000", courier_draft_id="apaczka-draft-1123"),

    # 1124 — InPost Kurier, error, plastik 3 zgrzewki
    _draft(_SEED_IDS[14], "1124", "Grzegorz Nowakowski",
           "inpost", "inpost_courier_standard", "error",
           packages_count=1, total_qty=3,
           order_items=[{"name": _P12, "quantity": 3}],
           receiver={"first_name": "Grzegorz", "last_name": "Nowakowski",
                     "email": "grzegorz@example.com", "phone": "+48615678901", "locker_id": ""},
           shipping_address={"street": "ul. Parkowa 6", "city": "Bydgoszcz", "post_code": "85-001"},
           error="InPost API 401: invalid token — check inpost_api_token in Key Vault"),
]


def seed(store: ShippingStore, clear: bool) -> None:
    if clear:
        for seed_id in _SEED_IDS:
            store.delete_draft(seed_id)
        print(f"  x  removed {len(_SEED_IDS)} seed drafts")

    for draft in TEST_DRAFTS:
        store.upsert_draft(draft)
        print(f"  ↑ #{draft['shopify_order_number']} {draft['customer_name']:<22s} [{draft['status']}]")

    print(f"\n✅  {len(TEST_DRAFTS)} seed drafts written")


def show_status(store: ShippingStore) -> None:
    drafts = store.list_drafts()
    if not drafts:
        print("  (no drafts)")
        return
    for d in drafts:
        print(f"  #{d['shopify_order_number']:6s}  {d['customer_name']:<22s}  {d['status']:<8s}  {d['courier']}")
    print(f"\n  Total: {len(drafts)}")


def clear_all(store: ShippingStore) -> None:
    drafts = store.list_drafts(limit=1000)
    for d in drafts:
        store.delete_draft(d["id"])
    print(f"  x  removed {len(drafts)} drafts")


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed test shipping drafts")
    parser.add_argument("--clear", action="store_true", help="Delete seed drafts then re-seed")
    parser.add_argument("--clear-all", action="store_true", help="Wipe ALL drafts then re-seed")
    parser.add_argument("--status", action="store_true", help="Show current storage state")
    args = parser.parse_args()

    store = make_store()

    if args.status:
        print("Shipping drafts status:")
        show_status(store)
        return

    if args.clear_all:
        print("Clearing all drafts...")
        clear_all(store)
        print("Seeding shipping drafts:")
        seed(store, clear=False)
        return

    print("Seedowanie shipping drafts:")
    seed(store, clear=args.clear)


if __name__ == "__main__":
    main()
