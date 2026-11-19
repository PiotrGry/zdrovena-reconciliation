"""A stateful fake of the Shopify fulfillment REST endpoints, on ``responses``.

Models what #541 depends on: fulfillment orders whose lines close once a
fulfillment covers them, the order's line items, and one email per
``notify_customer: true`` fulfillment. Repeat calls therefore behave as they
would against a real shop — a second fulfillment of the same lines is
impossible because nothing is left open.
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import responses

SHOP = "myshop.myshopify.com"
BASE = f"https://{SHOP}/admin/api/2024-01"

WATER = {
    "id": 501,
    "title": "Woda Humio 500ml x 12 butelek",
    "requires_shipping": True,
    "quantity": 2,
}
DEPOSIT = {"id": 502, "title": "Kaucja za butelki", "requires_shipping": False, "quantity": 24}


class FakeShopify:
    def __init__(self, order_id: str, line_items: list[dict[str, Any]]) -> None:
        self.order_id = order_id
        self.line_items = [dict(item) for item in line_items]
        # One fulfillment order holding every line, as Shopify creates by default.
        self.fulfillment_orders: list[dict[str, Any]] = [
            {
                "id": 9001,
                "status": "open",
                "line_items": [
                    {
                        "id": 7000 + index,
                        "line_item_id": item["id"],
                        "quantity": item["quantity"],
                        "fulfillable_quantity": item["quantity"],
                    }
                    for index, item in enumerate(self.line_items)
                ],
            }
        ]
        self.fulfillments: list[dict[str, Any]] = []
        self.fail_posts: list[int] = []  # HTTP statuses for the next POSTs
        self.fail_fulfillment_orders_get: int | None = None
        self._next_id = 777

    # ── observations ────────────────────────────────────────────────────────
    @property
    def emails(self) -> list[dict[str, Any]]:
        """One shipping email per notifying fulfillment, listing its line titles."""
        by_fo_line = {
            line["id"]: line["line_item_id"]
            for fo in self.fulfillment_orders
            for line in fo["line_items"]
        }
        titles = {item["id"]: item["title"] for item in self.line_items}
        return [
            {
                "items": [
                    titles[by_fo_line[line["id"]]]
                    for group in f["line_items_by_fulfillment_order"]
                    for line in group["fulfillment_order_line_items"]
                ],
                "tracking_info": f.get("tracking_info"),
            }
            for f in self.fulfillments
            if f["notify_customer"]
        ]

    @property
    def order_fulfilled(self) -> bool:
        return all(
            line["fulfillable_quantity"] == 0
            for fo in self.fulfillment_orders
            for line in fo["line_items"]
        )

    # ── handlers ────────────────────────────────────────────────────────────
    def _get_fulfillment_orders(self, request):
        if self.fail_fulfillment_orders_get:
            return (self.fail_fulfillment_orders_get, {}, json.dumps({"errors": "boom"}))
        return (200, {}, json.dumps({"fulfillment_orders": self.fulfillment_orders}))

    def _get_order(self, request):
        return (
            200,
            {},
            json.dumps({"order": {"id": self.order_id, "line_items": self.line_items}}),
        )

    def _post_fulfillment(self, request):
        if self.fail_posts:
            return (self.fail_posts.pop(0), {}, json.dumps({"errors": "Shopify down"}))
        body = json.loads(request.body)["fulfillment"]
        lines = {line["id"]: line for fo in self.fulfillment_orders for line in fo["line_items"]}
        for group in body["line_items_by_fulfillment_order"]:
            requested = group.get("fulfillment_order_line_items")
            targets = (
                [(lines[item["id"]], item["quantity"]) for item in requested]
                if requested
                else [(line, line["fulfillable_quantity"]) for line in lines.values()]
            )
            for line, quantity in targets:
                if quantity > line["fulfillable_quantity"]:
                    return (422, {}, json.dumps({"errors": "over-fulfilled"}))
                line["fulfillable_quantity"] -= quantity
        for fo in self.fulfillment_orders:
            if all(line["fulfillable_quantity"] == 0 for line in fo["line_items"]):
                fo["status"] = "closed"
        fulfillment_id = self._next_id
        self._next_id += 1
        self.fulfillments.append({"id": fulfillment_id, **body})
        return (201, {}, json.dumps({"fulfillment": {"id": fulfillment_id, "status": "success"}}))

    def register(self, mock: responses.RequestsMock) -> None:
        mock.add_callback(
            responses.GET,
            f"{BASE}/orders/{self.order_id}/fulfillment_orders.json",
            callback=self._get_fulfillment_orders,
        )
        mock.add_callback(
            responses.GET,
            re.compile(rf"{re.escape(BASE)}/orders/{self.order_id}\.json.*"),
            callback=self._get_order,
        )
        mock.add_callback(
            responses.POST, f"{BASE}/fulfillments.json", callback=self._post_fulfillment
        )


@contextmanager
def shopify_configured():
    """Shopify credentials and domain, as the shipping deps resolve them."""
    with (
        patch("zdrovena.api.routers.shipping.deps.get_secret", return_value="tok"),
        patch(
            "zdrovena.api.routers.shipping.deps._allowed_shopify_domains",
            return_value=frozenset([SHOP]),
        ),
    ):
        yield


@contextmanager
def fake_shopify(order_id: str, line_items: list[dict[str, Any]]):
    shop = FakeShopify(order_id, line_items)
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        shop.register(mock)
        with shopify_configured():
            yield shop
