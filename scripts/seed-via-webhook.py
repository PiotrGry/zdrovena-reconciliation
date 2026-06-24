#!/usr/bin/env python3
"""Send fake Shopify order webhooks to the local API to seed shipping drafts.

Tests the full pipeline: webhook → _create_draft → _calc_packages → storage.
HMAC validation is skipped when shopify_webhook_secret is not configured (dev mode).

Usage:
    python3 scripts/seed-via-webhook.py                  # send all 15 orders
    python3 scripts/seed-via-webhook.py --dry-run        # print payloads, don't send
    python3 scripts/seed-via-webhook.py --url http://localhost:8000

Requires the API to be running (docker compose up or DEV_MODE=local bash dev.sh).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

_P12 = "HUMIO - woda alkaliczna, 12 butelek"
_G12 = "HUMIO - woda alkaliczna, 12 butelek w szkle"


def _order(
    order_id: int,
    order_number: int,
    shipping_title: str,
    line_items: list[dict],
    first_name: str,
    last_name: str,
    email: str,
    phone: str,
    street: str,
    city: str,
    zip_code: str,
    note_attributes: list | None = None,
) -> dict:
    return {
        "id": order_id,
        "order_number": order_number,
        "email": email,
        "phone": phone,
        "shipping_lines": [{"title": shipping_title}],
        "shipping_address": {
            "first_name": first_name,
            "last_name": last_name,
            "address1": street,
            "address2": "",
            "city": city,
            "zip": zip_code,
            "phone": phone,
        },
        "line_items": line_items,
        "customer": {"first_name": first_name, "last_name": last_name, "email": email},
        "note_attributes": note_attributes or [],
    }


def _item(name: str, qty: int, grams: int = 5400) -> dict:
    return {
        "id": hash(name) & 0xFFFFFF,
        "name": name,
        "title": name,
        "quantity": qty,
        "grams": grams,
        "price": "89.99",
        "product_exists": True,
        "requires_shipping": True,
    }


# 15 test orders — mirrors seed-shipping-drafts.py combinations
ORDERS = [
    # 1110 — InPost Kurier, plastik 1 zgrzewka
    _order(
        50001110,
        1110,
        "InPost Kurier",
        [_item(_P12, 1)],
        "Marek",
        "Zielinski",
        "marek@example.com",
        "+48601234567",
        "ul. Wierzbowa 12",
        "Wroclaw",
        "50-001",
    ),
    # 1111 — InPost Kurier, plastik 3 zgrzewki → 1×3-pak
    _order(
        50001111,
        1111,
        "InPost Kurier",
        [_item(_P12, 3)],
        "Piotr",
        "Kowalczyk",
        "piotr@example.com",
        "+48602345678",
        "ul. Lipowa 5",
        "Gdansk",
        "80-001",
    ),
    # 1112 — InPost Kurier, plastik 5 zgrzewek → 1×3-pak + 1×2-pak
    _order(
        50001112,
        1112,
        "InPost Kurier",
        [_item(_P12, 5)],
        "Tomasz",
        "Wisniewski",
        "tomasz@example.com",
        "+48603456789",
        "ul. Brzozowa 8",
        "Krakow",
        "31-100",
    ),
    # 1113 — InPost Kurier, szkło 1 zgrzewka
    _order(
        50001113,
        1113,
        "InPost Kurier",
        [_item(_G12, 1)],
        "Anna",
        "Kowalska",
        "anna@example.com",
        "+48604567890",
        "ul. Kwiatowa 3",
        "Warszawa",
        "00-001",
    ),
    # 1114 — InPost Kurier, mixed 2×plastik + 1×szkło
    _order(
        50001114,
        1114,
        "InPost Kurier",
        [_item(_P12, 2), _item(_G12, 1)],
        "Beata",
        "Wojcik",
        "beata@example.com",
        "+48605678901",
        "ul. Prusa 3",
        "Lublin",
        "20-001",
    ),
    # 1115 — InPost Paczkomat, plastik 3 zgrzewki
    _order(
        50001115,
        1115,
        "InPost Paczkomat",
        [_item(_P12, 3)],
        "Katarzyna",
        "Nowak",
        "katarzyna@example.com",
        "+48606789012",
        "",
        "Krakow",
        "31-001",
        note_attributes=[{"name": "PickupPointId", "value": "KRK01M"}],
    ),
    # 1116 — InPost Paczkomat, szkło 2 zgrzewki
    _order(
        50001116,
        1116,
        "InPost Paczkomat",
        [_item(_G12, 2)],
        "Michał",
        "Dąbrowski",
        "michal@example.com",
        "+48607890123",
        "",
        "Warszawa",
        "02-001",
        note_attributes=[{"name": "PickupPointId", "value": "WAW88C"}],
    ),
    # 1117 — InPost Paczkomat, mixed 1×plastik + 2×szkło
    _order(
        50001117,
        1117,
        "InPost Paczkomat",
        [_item(_P12, 1), _item(_G12, 2)],
        "Zofia",
        "Maj",
        "zofia@example.com",
        "+48608901234",
        "",
        "Gdansk",
        "80-002",
        note_attributes=[{"name": "PickupPointId", "value": "GDA05B"}],
    ),
    # 1118 — Apaczka, plastik 6 zgrzewek → 2×3-pak
    _order(
        50001118,
        1118,
        "Apaczka kurier",
        [_item(_P12, 6)],
        "Krzysztof",
        "Lewandowski",
        "krzysztof@example.com",
        "+48609012345",
        "ul. Słoneczna 7",
        "Poznan",
        "60-001",
    ),
    # 1119 — Apaczka, szkło 3 zgrzewki
    _order(
        50001119,
        1119,
        "Apaczka kurier",
        [_item(_G12, 3)],
        "Aleksandra",
        "Wójcik",
        "aleksandra@example.com",
        "+48610123456",
        "ul. Różana 2",
        "Wroclaw",
        "51-001",
    ),
    # 1120 — InPost Kurier, plastik 3 zgrzewki (duplicate — becomes 2nd pending)
    _order(
        50001120,
        1120,
        "InPost Kurier",
        [_item(_P12, 3)],
        "Paweł",
        "Kaminski",
        "pawel@example.com",
        "+48611234567",
        "ul. Jagiellońska 4",
        "Warszawa",
        "03-001",
    ),
    # 1121 — InPost Kurier, szkło 1 zgrzewka
    _order(
        50001121,
        1121,
        "InPost Kurier",
        [_item(_G12, 1)],
        "Monika",
        "Szymanska",
        "monika@example.com",
        "+48612345678",
        "ul. Piękna 9",
        "Krakow",
        "31-200",
    ),
    # 1122 — InPost Paczkomat, mixed 3×plastik + 1×szkło
    _order(
        50001122,
        1122,
        "InPost Paczkomat",
        [_item(_P12, 3), _item(_G12, 1)],
        "Rafał",
        "Mazur",
        "rafal@example.com",
        "+48613456789",
        "",
        "Lodz",
        "90-001",
        note_attributes=[{"name": "PickupPointId", "value": "LDZ02A"}],
    ),
    # 1123 — Apaczka, plastik 4 zgrzewki → 1×3-pak + 1×1-pak
    _order(
        50001123,
        1123,
        "Apaczka kurier",
        [_item(_P12, 4)],
        "Magdalena",
        "Kaczmarek",
        "magdalena@example.com",
        "+48614567890",
        "ul. Mickiewicza 11",
        "Szczecin",
        "70-001",
    ),
    # 1124 — InPost Kurier, plastik 3 zgrzewki
    _order(
        50001124,
        1124,
        "InPost Kurier",
        [_item(_P12, 3)],
        "Grzegorz",
        "Nowakowski",
        "grzegorz@example.com",
        "+48615678901",
        "ul. Parkowa 6",
        "Bydgoszcz",
        "85-001",
    ),
]


def send(url: str, order: dict, dry_run: bool) -> bool:
    payload = json.dumps(order).encode()
    num = order["order_number"]
    if dry_run:
        print(
            f"  [dry-run] #{num} {order['shipping_lines'][0]['title']} — {len(order['line_items'])} line item(s)"
        )
        return True
    req = urllib.request.Request(
        f"{url}/webhooks/shopify/order-created",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            ok = resp.status == 200
            print(f"  {'✓' if ok else '✗'} #{num} → {resp.status}")
            return ok
    except urllib.error.HTTPError as e:
        print(f"  ✗ #{num} → HTTP {e.code}: {e.read().decode()[:120]}")
        return False
    except Exception as e:
        print(f"  ✗ #{num} → {e}")
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed shipping drafts via webhook API")
    parser.add_argument("--url", default="http://localhost:8000", help="API base URL")
    parser.add_argument("--dry-run", action="store_true", help="Print payloads, don't send")
    parser.add_argument("--delay", type=float, default=0.1, help="Seconds between requests")
    args = parser.parse_args()

    print(f"Sending {len(ORDERS)} orders to {args.url} ...")
    ok = 0
    for order in ORDERS:
        if send(args.url, order, args.dry_run):
            ok += 1
        if not args.dry_run and args.delay:
            time.sleep(args.delay)

    print(f"\n{'✅' if ok == len(ORDERS) else '⚠️ '}  {ok}/{len(ORDERS)} orders sent successfully")
    if ok < len(ORDERS):
        sys.exit(1)


if __name__ == "__main__":
    main()
