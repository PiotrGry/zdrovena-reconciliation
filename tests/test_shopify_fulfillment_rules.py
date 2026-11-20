"""The two #541 rules: what a deposit line is, and which Shopify carrier a draft uses."""

from __future__ import annotations

import pytest

from zdrovena.common.apaczka import APACZKA_SERVICE_CATALOG
from zdrovena.shipping.domain.shopify_fulfillment import (
    SHOPIFY_CARRIER_TABLE,
    UNKNOWN_CARRIER_TRACKING_URL,
    is_deposit_line,
    plan_fulfillments,
    shopify_tracking_carrier,
    tracking_info,
)

# Names from Shopify's supported tracking-company list the task allows (#541).
SHOPIFY_SUPPORTED = {
    "InPost",
    "UPS",
    "DPD",
    "DHL Parcel",
    "Poczta Polska",
    "FedEx",
    "GLS",
    "Orlen Paczka",
    "Packeta",
}


class TestIsDepositLine:
    @pytest.mark.parametrize(
        "line",
        [
            {"title": "Kaucja za butelki", "requires_shipping": True},
            {"title": "KAUCJA", "requires_shipping": True},
            {"title": "Opłata", "name": "Opłata - kaucja zwrotna"},
            {"title": "Butelki", "variant_title": "Kaucja 0,50 zł"},
            {"title": "Zwrotne opakowanie", "requires_shipping": False},
        ],
    )
    def test_deposit(self, line):
        assert is_deposit_line(line) is True

    @pytest.mark.parametrize(
        "line",
        [
            {"title": "Woda Humio 500ml x 12 butelek", "requires_shipping": True},
            {"title": "Woda Humio 500ml x 12 butelek"},  # missing ≠ False
            {"title": "", "requires_shipping": None},
            {},
        ],
    )
    def test_product(self, line):
        assert is_deposit_line(line) is False


class TestCarrierTable:
    def test_every_apaczka_service_maps_to_a_supported_carrier(self):
        for service_id in APACZKA_SERVICE_CATALOG:
            carrier = shopify_tracking_carrier(
                {"courier": "apaczka", "apaczka_service_id": service_id}
            )
            assert carrier is not None, f"service {service_id} missing from table"
            assert carrier.company in SHOPIFY_SUPPORTED

    def test_table_has_no_apaczka_service_outside_the_catalog(self):
        table_ids = {key for courier, key in SHOPIFY_CARRIER_TABLE if courier == "apaczka"}
        assert table_ids == set(APACZKA_SERVICE_CATALOG)

    def test_every_company_in_the_table_is_supported(self):
        assert {c.company for c in SHOPIFY_CARRIER_TABLE.values()} <= SHOPIFY_SUPPORTED

    @pytest.mark.parametrize(
        ("service_id", "company"),
        [
            ("1", "UPS"),
            ("21", "DPD"),
            ("60", "Poczta Polska"),
            ("82", "DHL Parcel"),
            ("86", "DHL Parcel"),
            ("151", "FedEx"),
            ("202", "GLS"),
            ("50", "Orlen Paczka"),
            ("314", "Packeta"),
        ],
    )
    def test_apaczka_service_examples(self, service_id, company):
        draft = {"courier": "apaczka", "apaczka_service_id": service_id}
        assert tracking_info(draft, "WB1")["company"] == company

    def test_inpost(self):
        info = tracking_info({"courier": "inpost"}, "620123")
        assert info == {
            "number": "620123",
            "company": "InPost",
            "url": "https://inpost.pl/sledzenie-przesylek?number=620123",
        }

    @pytest.mark.parametrize(
        ("carrier_id", "company"),
        [("INPOST", "InPost"), ("DPD", "DPD"), ("ORLEN", "Orlen Paczka"), ("dhl", "DHL Parcel")],
    )
    def test_allegro_delivery_uses_the_actual_carrier(self, carrier_id, company):
        draft = {"courier": "allegro_delivery", "allegro_carrier_id": carrier_id}
        assert tracking_info(draft, "X")["company"] == company

    def test_every_carrier_link_contains_the_number(self):
        for carrier in SHOPIFY_CARRIER_TABLE.values():
            url = carrier.tracking_url("NUM123")
            assert url.startswith("https://") and "NUM123" in url

    @pytest.mark.parametrize(
        ("draft", "company"),
        [
            ({"courier": "apaczka", "apaczka_service_id": "999"}, "Apaczka"),
            ({"courier": "apaczka"}, "Apaczka"),
            ({"courier": "allegro_delivery", "allegro_carrier_id": "ALLEGRO"}, "Allegro"),
            ({"courier": "allegro_delivery"}, "Allegro Delivery"),
            ({"courier": "pigeon"}, "pigeon"),
        ],
    )
    def test_unknown_carrier_sends_name_and_url(self, draft, company):
        info = tracking_info(draft, "N1")
        assert info["company"] == company
        assert info["url"] == UNKNOWN_CARRIER_TRACKING_URL.format(number="N1")


def _fo(lines, status="open", fo_id=1):
    return {
        "id": fo_id,
        "status": status,
        "line_items": [
            {"id": 100 + li, "line_item_id": li, "fulfillable_quantity": qty} for li, qty in lines
        ],
    }


WATER = {"id": 1, "title": "Woda", "requires_shipping": True}
DEPOSIT = {"id": 2, "title": "Kaucja", "requires_shipping": False}


class TestPlanFulfillments:
    def test_water_and_deposit_split(self):
        plan = plan_fulfillments([_fo([(1, 2), (2, 24)])], [WATER, DEPOSIT])

        assert [(p.kind, p.notify_customer, p.with_tracking) for p in plan] == [
            ("products", True, True),
            ("deposit", False, False),
        ]
        assert plan[0].line_items_by_fulfillment_order == [
            {
                "fulfillment_order_id": 1,
                "fulfillment_order_line_items": [{"id": 101, "quantity": 2}],
            }
        ]
        assert plan[1].line_items_by_fulfillment_order == [
            {
                "fulfillment_order_id": 1,
                "fulfillment_order_line_items": [{"id": 102, "quantity": 24}],
            }
        ]

    def test_deposit_payload_has_no_tracking(self):
        plan = plan_fulfillments([_fo([(2, 1)])], [DEPOSIT])
        payload = plan[0].payload({"number": "N", "company": "InPost"})
        assert payload["fulfillment"]["notify_customer"] is False
        assert "tracking_info" not in payload["fulfillment"]

    def test_only_water(self):
        plan = plan_fulfillments([_fo([(1, 2)])], [WATER])
        assert [p.kind for p in plan] == ["products"]

    def test_closed_or_exhausted_lines_are_ignored(self):
        assert plan_fulfillments([_fo([(1, 2)], status="closed")], [WATER]) == []
        assert plan_fulfillments([_fo([(1, 0)])], [WATER]) == []

    def test_unclassifiable_lines_go_silent_with_tracking(self):
        plan = plan_fulfillments([_fo([(1, 2), (3, 1)])], [WATER])  # line 3 unknown

        assert len(plan) == 1
        assert plan[0].kind == "all"
        assert plan[0].notify_customer is False
        assert plan[0].with_tracking is True

    def test_missing_order_lines_go_silent(self):
        plan = plan_fulfillments([_fo([(1, 2)])], None)
        assert [(p.kind, p.notify_customer) for p in plan] == [("all", False)]
