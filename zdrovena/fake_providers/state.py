"""In-memory state owned by the provider emulators.

The state deliberately stores provider resources, not application DTOs.  This
keeps the emulators independent from ``zdrovena.common`` client builders.
"""

from __future__ import annotations

from typing import Any


def sample_allegro_order(order_id: str = "fake-order-1") -> dict[str, Any]:
    return {
        "id": order_id,
        "status": "READY_FOR_PROCESSING",
        "fulfillment": {"status": "NEW"},
        "lineItems": [
            {
                "id": "line-1",
                "offer": {"name": "HUMIO"},
                "quantity": 1,
                "price": {"amount": "29.99", "currency": "PLN"},
                "tax": {"rate": "23"},
            }
        ],
        "buyer": {
            "email": "buyer@example.test",
            "firstName": "Fake",
            "lastName": "Buyer",
            "address": {
                "street": "Prosta 1",
                "postCode": "00-001",
                "city": "Warszawa",
                "countryCode": "PL",
            },
        },
        "delivery": {"method": {"name": "Fake delivery"}, "cost": {"amount": "0.00"}},
        "summary": {"totalToPay": {"amount": "29.99", "currency": "PLN"}},
    }


class FakeProviderState:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.scenarios: dict[str, str] = {}
        self.allegro_orders: dict[str, dict[str, Any]] = {"fake-order-1": sample_allegro_order()}
        self.allegro_shipments: dict[str, dict[str, Any]] = {}
        self.allegro_commands: dict[str, dict[str, Any]] = {}
        self.allegro_pickup_commands: dict[str, dict[str, Any]] = {}
        self.allegro_invoices: dict[str, dict[str, Any]] = {}
        self.allegro_dispatches: dict[str, dict[str, Any]] = {}
        self.inpost_shipments: dict[str, dict[str, Any]] = {}
        self.inpost_dispatches: dict[str, dict[str, Any]] = {}
        self.apaczka_orders: dict[str, dict[str, Any]] = {}
        self.fakturownia_invoices: dict[str, dict[str, Any]] = {}
        self.counters: dict[str, int] = {}

    def next_id(self, prefix: str) -> str:
        self.counters[prefix] = self.counters.get(prefix, 0) + 1
        return f"{prefix}-{self.counters[prefix]:04d}"

    def scenario(self, provider: str, operation: str) -> str | None:
        return self.scenarios.get(f"{provider}:{operation}")


STATE = FakeProviderState()
