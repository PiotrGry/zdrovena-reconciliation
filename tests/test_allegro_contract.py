"""Contract tests pinning our Allegro parsers/mapper to REAL production fixtures.

Fixtures under tests/fixtures/allegro/ are genuine production responses. They are
the ground truth: if a parser diverges from them, the test (and the audit) treats
the fixture as correct and the code as suspect.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from zdrovena.common.allegro import AllegroClient
from zdrovena.common.allegro_mapper import allegro_to_shopify_order

_FIXTURES = Path(__file__).parent / "fixtures" / "allegro"


def _load(name: str) -> dict:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def _client() -> AllegroClient:
    return AllegroClient(
        client_id="cid", client_secret="csec", refresh_token="rt", env="prod"
    )


class TestAllegroCheckoutFormsList:
    def test_parse_checkout_forms_list(self):
        data = _load("checkout-forms-list.json")
        # Our client parser: list_orders() returns data["checkoutForms"].
        with patch.object(AllegroClient, "_get", return_value=data):
            forms = _client().list_orders(status="READY_FOR_PROCESSING")

        assert len(forms) >= 3
        for form in forms:
            for field in ("id", "status", "buyer", "lineItems", "delivery", "payment"):
                assert field in form, f"missing {field!r} in checkout form"


class TestAllegroCheckoutFormDetail:
    def test_parse_checkout_form_detail(self):
        form = _load("checkout-form-detail.json")

        assert form["status"] == "READY_FOR_PROCESSING"

        buyer = form["buyer"]
        assert buyer["address"]["city"] == "Sieradz"
        # Buyer address uses `postCode` (delivery address uses `zipCode` — different!).
        assert buyer["address"]["postCode"] == "98-200"

        line_items = form["lineItems"]
        assert len(line_items) >= 1
        assert line_items[0]["offer"]["name"]
        assert line_items[0]["quantity"] >= 1

        delivery = form["delivery"]
        assert delivery["method"]["name"] == "Allegro One Box, DPD"
        assert delivery["method"]["id"]
        # Delivery address uses `zipCode`, not `zip` / `postCode`.
        assert delivery["address"]["zipCode"] == "98-200"

        assert form["payment"]["type"] == "ONLINE"


class TestAllegroCheckoutFormInvoices:
    def test_parse_checkout_form_invoices(self):
        data = _load("checkout-form-invoices.json")
        # Our client parser: list_order_invoices() returns data["invoices"].
        with patch.object(AllegroClient, "_get", return_value=data):
            invoices = _client().list_order_invoices("order-1")

        assert len(invoices) >= 1
        for inv in invoices:
            # CRIT fix: the field is `invoiceNumber`, NOT `number`.
            assert "invoiceNumber" in inv
            assert "number" not in inv
            assert inv["invoiceNumber"]


class TestAllegroCheckoutFormShipments:
    def test_parse_checkout_form_shipments(self):
        data = _load("checkout-form-shipments.json")
        # Fixture is an empty shipments list — parser must degrade to [].
        with patch.object(AllegroClient, "_get", return_value=data):
            shipments = _client().get_shipments("order-1")

        assert shipments == []


class TestAllegroMapperOnRealOrder:
    def test_allegro_mapper_maps_real_order_to_shopify(self):
        form = _load("checkout-form-detail.json")
        order = allegro_to_shopify_order(form)

        assert order["id"] == form["id"]
        assert order["email"] == "abqsn58id1+48f7f6069@allegromail.pl"
        # Phone sourced from delivery.address.phoneNumber.
        assert order["phone"] == "+48697102046"

        shipping = order["shipping_address"]
        assert shipping["first_name"] == "Agnieszka"
        assert shipping["last_name"] == "Wojtczak"
        assert shipping["name"] == "Agnieszka Wojtczak"
        assert shipping["address1"] == "Różana, 17"
        assert shipping["city"] == "Sieradz"
        assert shipping["zip"] == "98-200"
        assert shipping["country_code"] == "PL"

        # Line items mapped with sku from offer.external.id ("PET").
        assert len(order["line_items"]) == 1
        li = order["line_items"][0]
        assert li["name"] == "HUMIO - Alkaliczna Woda Humusowa 500ml x 12"
        assert li["quantity"] == 2
        assert li["sku"] == "PET"

        # Pickup point + Ship-with-Allegro method id preserved as note attributes.
        attrs = {a["name"]: a["value"] for a in order["note_attributes"]}
        assert attrs["PickupPointId"] == "AL012ESI"
        assert attrs["AllegroDeliveryMethodId"] == form["delivery"]["method"]["id"]

        # Shipping title carries the locker id so extract_locker_id_from_title works.
        title = order["shipping_lines"][0]["title"]
        assert "Allegro One Box, DPD" in title
        assert "AL012ESI" in title

        # No None leaked into fields the downstream pipeline reads.
        for key in ("id", "email", "phone", "shipping_lines", "line_items"):
            assert order[key] is not None
