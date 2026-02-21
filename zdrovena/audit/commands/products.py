"""
``zdrovena products`` – List all products from Fakturownia.
"""

from __future__ import annotations

import argparse

from zdrovena.audit.api import get_client, fetch_products
from zdrovena.common.formatting import BOLD, RESET, GREEN, DIM, RED


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "products",
        help="Wyświetl listę produktów z Fakturowni",
        description="Pobiera i wyświetla wszystkie produkty z Fakturowni.",
    )
    p.add_argument("--active-only", action="store_true", help="Tylko aktywne produkty")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> None:
    client = get_client()
    print("Pobieranie produktów...")
    products = fetch_products(client)

    if args.active_only:
        products = [p for p in products if not p.get("disabled")]

    print(f"\n{BOLD}Produkty ({len(products)}):{RESET}\n")
    for p in products:
        status = f"{GREEN}[AKTYWNY]{RESET}" if not p.get("disabled") else f"{RED}[WYŁĄCZONY]{RESET}"
        price = p.get("price_net") or p.get("price_gross") or "—"
        print(f"  {status} {p.get('name', '?')}  {DIM}(id={p['id']}, cena={price}){RESET}")
