"""zdrovena.common.kaucja — jedno kanoniczne źródło kwoty kaucji.

Kaucja (deposit) za opakowania zwrotne ma DOKŁADNIE jedno źródło prawdy:
natywne pole Allegro ``lineItems[].deposit.price.amount`` (wartość
PER-UNIT) przemnożone przez ``quantity`` i zsumowane po wszystkich
pozycjach zamówienia.

Dlaczego jedno źródło:
    Wcześniej istniały DWA niezależne mechanizmy liczenia kaucji, które
    mogły dawać różne kwoty dla tej samej faktury:
      1. mapper (allegro_invoice_mapper) — natywny deposit z Allegro,
      2. patcher (fakturownia_patcher) — heurystyka nazwa-produktu × 0,50 PLN
         (count_pet_bottles z bottles.py).
    Rozbieżność oznaczała, że "Do zapłaty" na fakturze mogło NIE zgadzać
    się z Allegro ``summary.totalToPay``. PR-13/PR-27: heurystyka NIE jest
    już źródłem kwoty — służy wyłącznie jako cross-check (log ostrzeżenia).

To NIE zmienia mechanizmu KSeF/Rozliczenie — kwota nadal trafia do
``settlement_positions`` jako ``charge`` w niezmienionym formacie.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any


def calculate_kaucja(order: dict[str, Any]) -> Decimal:
    """Zsumuj kaucję zamówienia z natywnych pól Allegro (kanoniczne źródło).

    ``lineItems[].deposit.price.amount`` to wartość PER-UNIT — mnożymy przez
    ``quantity`` (potwierdzone na realnym zamówieniu: quantity=2,
    deposit=6.00, totalToPay=158.00 = (73.00 + 6.00) * 2). Pozycje bez
    ``deposit`` (np. szkło) nie wnoszą kaucji.

    Zwraca Decimal (nie sformatowany string) — formatowanie do 2 miejsc
    należy do wołającego, tuż przed wysyłką.
    """
    total = Decimal("0")
    for item in order.get("lineItems") or []:
        deposit = item.get("deposit")
        if not deposit:
            continue
        quantity = int(item.get("quantity", 1) or 1)
        unit_deposit = Decimal(str((deposit.get("price") or {}).get("amount", "0")))
        total += unit_deposit * quantity
    return total
