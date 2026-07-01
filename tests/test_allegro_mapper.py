"""Tests for zdrovena.common.allegro_mapper.allegro_to_shopify_order.

The mapper converts an Allegro checkout-form payload into a Shopify-like
order dict so it can flow through the existing _create_draft() pipeline
(reusing courier picking, package calculation, phone/address normalisation).

We do NOT attempt to reproduce every Shopify field — only the ones
_create_draft() reads: id, order_number, shipping_lines[].title,
shipping_address, email, phone, line_items[].name/quantity/sku,
note_attributes (for PickupPointId).
"""

from __future__ import annotations

from zdrovena.common.allegro_mapper import allegro_to_shopify_order


def _base_form() -> dict:
    return {
        "id": "af123",
        "buyer": {
            "email": "buyer@example.com",
            "firstName": "Jan",
            "lastName": "Kowalski",
            "phoneNumber": "+48 601 111 111",
            "login": "jankow",
        },
        "delivery": {
            "address": {
                "firstName": "Jan",
                "lastName": "Kowalski",
                "street": "ul. Kwiatowa 5/3",
                "city": "Warszawa",
                "zipCode": "00-001",
                "phoneNumber": "+48601111111",
                "countryCode": "PL",
            },
            "method": {"name": "InPost Paczkomaty 24/7", "id": "m-1"},
            "pickupPoint": {"id": "WAW123A", "name": "Paczkomat WAW123A"},
            "cost": {"amount": "0.00", "currency": "PLN"},
        },
        "lineItems": [
            {
                "id": "li-1",
                "offer": {
                    "id": "off1",
                    "name": "HUMIO - woda alkaliczna, 12 butelek w szkle",
                    "external": {"id": "HUMIO-SZKLO-12-001"},
                },
                "quantity": 1,
                "boughtAt": "2026-06-01T10:00:00Z",
            }
        ],
        "messageToSeller": "",
    }


class TestBasicFields:
    def test_id_and_order_number_from_allegro_id(self):
        o = allegro_to_shopify_order(_base_form())
        assert o["id"] == "af123"
        # order_number for display — use the same allegro id
        assert str(o["order_number"]) == "af123"

    def test_email_from_buyer(self):
        o = allegro_to_shopify_order(_base_form())
        assert o["email"] == "buyer@example.com"

    def test_customer_first_last_from_buyer(self):
        o = allegro_to_shopify_order(_base_form())
        c = o.get("customer") or {}
        assert c.get("first_name") == "Jan"
        assert c.get("last_name") == "Kowalski"


class TestShippingAddress:
    def test_address_fields_copied(self):
        o = allegro_to_shopify_order(_base_form())
        a = o["shipping_address"]
        assert a["first_name"] == "Jan"
        assert a["last_name"] == "Kowalski"
        assert a["address1"] == "ul. Kwiatowa 5/3"
        assert a["city"] == "Warszawa"
        assert a["zip"] == "00-001"
        assert a["phone"] == "+48601111111"

    def test_paczkomat_pickup_point_maps_to_address2_and_note(self):
        o = allegro_to_shopify_order(_base_form())
        a = o["shipping_address"]
        # locker id available via note_attributes AND (fallback) address2
        na = {n["name"]: n["value"] for n in o.get("note_attributes") or []}
        assert na.get("PickupPointId") == "WAW123A"
        assert a.get("address2") in {"WAW123A", ""}  # tolerate either

    def test_no_pickup_point_leaves_note_empty(self):
        form = _base_form()
        form["delivery"]["pickupPoint"] = None
        o = allegro_to_shopify_order(form)
        na = {n["name"]: n["value"] for n in o.get("note_attributes") or []}
        assert "PickupPointId" not in na or not na["PickupPointId"]


class TestShippingLines:
    def test_shipping_line_title_from_delivery_method(self):
        o = allegro_to_shopify_order(_base_form())
        sl = o["shipping_lines"]
        assert len(sl) == 1
        assert "inpost" in sl[0]["title"].lower() or "paczkomat" in sl[0]["title"].lower()

    def test_shipping_line_kurier_when_no_pickup_point(self):
        form = _base_form()
        form["delivery"]["method"]["name"] = "Kurier InPost"
        form["delivery"]["pickupPoint"] = None
        o = allegro_to_shopify_order(form)
        assert "kurier" in o["shipping_lines"][0]["title"].lower()

    def test_shipping_line_apaczka_courier_when_dpd(self):
        form = _base_form()
        form["delivery"]["method"]["name"] = "DPD Kurier"
        form["delivery"]["pickupPoint"] = None
        o = allegro_to_shopify_order(form)
        # should NOT contain 'inpost' → _pick_courier will pick apaczka
        assert "inpost" not in o["shipping_lines"][0]["title"].lower()


class TestLineItems:
    def test_line_items_use_offer_external_id_as_sku(self):
        o = allegro_to_shopify_order(_base_form())
        li = o["line_items"]
        assert li[0]["sku"] == "HUMIO-SZKLO-12-001"
        assert li[0]["name"] == "HUMIO - woda alkaliczna, 12 butelek w szkle"
        assert li[0]["quantity"] == 1

    def test_line_items_fallback_sku_from_offer_id(self):
        form = _base_form()
        form["lineItems"][0]["offer"].pop("external", None)
        o = allegro_to_shopify_order(form)
        assert o["line_items"][0]["sku"] == "off1"

    def test_multiple_line_items(self):
        form = _base_form()
        form["lineItems"].append(
            {
                "id": "li-2",
                "offer": {
                    "id": "off2",
                    "name": "HUMIO 6 PET",
                    "external": {"id": "HUMIO-PET-6-001"},
                },
                "quantity": 2,
                "boughtAt": "2026-06-01T10:00:00Z",
            }
        )
        o = allegro_to_shopify_order(form)
        assert len(o["line_items"]) == 2
        assert o["line_items"][1]["quantity"] == 2


class TestPhone:
    def test_phone_from_delivery_address(self):
        o = allegro_to_shopify_order(_base_form())
        assert o["shipping_address"]["phone"] == "+48601111111"

    def test_phone_falls_back_to_buyer(self):
        form = _base_form()
        form["delivery"]["address"]["phoneNumber"] = ""
        o = allegro_to_shopify_order(form)
        assert o["shipping_address"]["phone"] == "+48 601 111 111"


# ── delivery.method.id propagation (for Ship with Allegro) ────────────────────


def test_delivery_method_id_added_to_note_attributes():
    """The mapper must expose delivery.method.id so _create_draft can save it.

    Ship with Allegro needs deliveryMethodId to call create-commands.
    """
    from zdrovena.common.allegro_mapper import allegro_to_shopify_order

    form = {
        "id": "ORD-1",
        "buyer": {"firstName": "Jan", "lastName": "K", "email": "j@k.pl"},
        "delivery": {
            "method": {"id": "svc-inpost-locker", "name": "Allegro InPost Paczkomat"},
            "address": {"street": "Kwiatowa 1", "city": "Warszawa", "zipCode": "00-001"},
            "pickupPoint": {"id": "WAW01A", "name": "Warszawa 01A"},
        },
        "lineItems": [],
    }
    order = allegro_to_shopify_order(form)
    note_attrs = {a["name"]: a["value"] for a in order["note_attributes"]}
    assert note_attrs.get("AllegroDeliveryMethodId") == "svc-inpost-locker"
