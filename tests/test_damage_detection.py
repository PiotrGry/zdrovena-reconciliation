from __future__ import annotations

from datetime import datetime, timezone

from zdrovena.api.damage_detection import (
    extract_inpost_tracking,
    is_allowed_inpost_sender,
    scan_allegro_damage_cases,
    scan_zoho_damage_cases,
)
from zdrovena.common.damage_store import DamageStore
from zdrovena.common.shipping_store import ShippingStore


def _allegro_draft() -> dict:
    return {
        "id": "draft-1648",
        "created_at": "2026-07-14T08:00:00Z",
        "source": "allegro",
        "external_order_id": "0af59a90-7f08-11f1-b5bc-398519c00320",
        "shopify_order_number": "1648",
        "customer_name": "Jan Kowalski",
        "receiver": {"email": "jan@example.com", "first_name": "Jan"},
        "courier": "allegro_delivery",
        "status": "pending",
        "tracking_number": None,
    }


class AllegroStub:
    def get_shipments(self, order_id):
        assert order_id == "0af59a90-7f08-11f1-b5bc-398519c00320"
        return [
            {
                "id": "shipment-1",
                "waybill": "A0052HFZF6",
                "carrierId": "ALLEGRO",
            }
        ]

    def get_tracking_history(self, carrier_id, waybills):
        assert carrier_id == "ALLEGRO"
        assert waybills == ["A0052HFZF6"]
        return {
            "carrierId": carrier_id,
            "waybills": [
                {
                    "waybill": "A0052HFZF6",
                    "trackingDetails": {
                        "statuses": [
                            {
                                "occurredAt": "2026-07-15T13:40:42.072Z",
                                "code": "ISSUE",
                                "description": "Parcel has been damaged",
                            },
                            {
                                "occurredAt": "2026-07-15T19:02:56.982Z",
                                "code": "RETURNED",
                                "description": "Parcel has been returned to sender",
                            },
                        ]
                    },
                }
            ],
        }


def test_allegro_scans_full_history_and_is_idempotent(tmp_path):
    shipping = ShippingStore(local_root=tmp_path / "shipping")
    damage = DamageStore(local_root=tmp_path / "damage")
    shipping.upsert_draft(_allegro_draft())

    first = scan_allegro_damage_cases(
        client=AllegroStub(), shipping_store=shipping, damage_store=damage
    )
    second = scan_allegro_damage_cases(
        client=AllegroStub(), shipping_store=shipping, damage_store=damage
    )

    assert first["created"] == 1
    assert second["created"] == 0
    cases = damage.list_cases()
    assert len(cases) == 1
    assert cases[0]["tracking_number"] == "A0052HFZF6"
    assert cases[0]["status"] == "needs_review"
    assert cases[0]["classification"] == "damage"
    assert cases[0]["order_number"] == "1648"
    assert shipping.get_draft("draft-1648")["tracking_number"] == "A0052HFZF6"


class AllegroDelayStub(AllegroStub):
    def get_tracking_history(self, carrier_id, waybills):
        return {
            "carrierId": carrier_id,
            "waybills": [
                {
                    "waybill": waybills[0],
                    "trackingDetails": {
                        "statuses": [
                            {
                                "occurredAt": "2026-07-15T13:40:42.072Z",
                                "code": "ISSUE",
                                "description": "Parcel may be delivered with delay",
                            }
                        ]
                    },
                }
            ],
        }


def test_allegro_delay_issue_is_not_a_damage_case(tmp_path):
    shipping = ShippingStore(local_root=tmp_path / "shipping")
    damage = DamageStore(local_root=tmp_path / "damage")
    shipping.upsert_draft(_allegro_draft())

    stats = scan_allegro_damage_cases(
        client=AllegroDelayStub(), shipping_store=shipping, damage_store=damage
    )

    assert stats["issues"] == 0
    assert damage.list_cases() == []


def test_inpost_sender_and_subject_first_tracking_extraction():
    assert is_allowed_inpost_sender("uszkodzeniagda@inpost.pl")
    assert is_allowed_inpost_sender("uszkodzoneldz@inpost.pl")
    assert is_allowed_inpost_sender("dyspozycje_biznes@inpost.pl")
    assert not is_allowed_inpost_sender("alerts@example.com")
    assert (
        extract_inpost_tracking(
            "Uszkodzona 123456789012345678901234",
            "inny numer 999999999999999999999999",
        )
        == "123456789012345678901234"
    )


class ZohoStub:
    def search_damage_notifications(self, since_ms=0):
        assert since_ms >= 0
        return [
            {
                "messageId": "msg-1",
                "fromAddress": "uszkodzeniakat@inpost.pl",
                "subject": "Uszkodzona przesyłka 123456789012345678901234",
                "content": "Przesyłka została uszkodzona w transporcie.",
                "receivedTime": 1_752_592_442_000,
                "hasAttachment": "1",
            },
            {
                "messageId": "msg-evil",
                "fromAddress": "alerts@example.com",
                "subject": "Uszkodzona przesyłka 999999999999999999999999",
                "content": "uszkodzona",
                "receivedTime": 1_752_592_443_000,
            },
        ]


class UncorrelatedZohoStub:
    def search_damage_notifications(self, since_ms=0):
        del since_ms
        return [
            {
                "messageId": "msg-apaczka",
                "fromAddress": "uszkodzeniagda@inpost.pl",
                "subject": "Przesyłka Uszkodzona 630015608680156036357414",
                "content": "Przesyłka została uszkodzona w transporcie.",
                "receivedTime": 1_752_592_442_000,
            }
        ]


class ProviderLookupZohoStub:
    def search_damage_notifications(self, since_ms=0):
        del since_ms
        received = int(datetime(2026, 7, 16, 12, tzinfo=timezone.utc).timestamp() * 1000)
        return [
            {
                "messageId": "msg-report",
                "folderId": "folder-1",
                "fromAddress": "uszkodzeniagda@inpost.pl",
                "subject": "Przesyłka Uszkodzona 630015608680156036357414",
                "content": "Przesyłka została uszkodzona w transporcie.",
                "receivedTime": received,
            }
        ]


class InPostLookupStub:
    def find_shipment_by_tracking(self, tracking):
        assert tracking == "630015608680156036357414"
        return {
            "id": 12345,
            "tracking_number": tracking,
            "reference": None,
            "service": "inpost_courier_standard",
            "receiver": {
                "first_name": "Wojciech",
                "last_name": "Religa",
                "email": "w.religa@post.pl",
                "phone": "+48 600 100 200",
                "address": {
                    "street": "Kwiatowa",
                    "building_number": "2",
                    "city": "Warszawa",
                    "post_code": "00-001",
                },
            },
        }


class ApaczkaStub:
    calls = 0

    def list_orders(self, *, page, limit):
        self.calls += 1
        assert limit == 25
        if page > 1:
            return []
        return [
            {
                "id": 991,
                "externalId": "1648",
                "waybill_number": "630015608680156036357414",
                "service_name": "InPost Kurier",
                "receiver": {
                    "name": "Jan Kowalski",
                    "email": "jan@example.com",
                },
            }
        ]


def test_zoho_scan_filters_sender_and_correlates_tracking(tmp_path):
    shipping = ShippingStore(local_root=tmp_path / "shipping")
    damage = DamageStore(local_root=tmp_path / "damage")
    draft = _allegro_draft()
    draft.update(
        {
            "id": "inpost-draft",
            "source": "shopify",
            "tracking_number": "123456789012345678901234",
            "courier": "inpost",
        }
    )
    shipping.upsert_draft(draft)

    stats = scan_zoho_damage_cases(client=ZohoStub(), shipping_store=shipping, damage_store=damage)

    assert stats["matched"] == 1
    assert stats["created"] == 1
    case = damage.list_cases()[0]
    assert case["shipping_draft_id"] == "inpost-draft"
    assert case["sources"] == ["zoho_inpost"]
    assert isinstance(damage.get_state("zoho_received_cursor_ms"), str)


def test_zoho_scan_correlates_unstored_tracking_through_apaczka(tmp_path):
    shipping = ShippingStore(local_root=tmp_path / "shipping")
    damage = DamageStore(local_root=tmp_path / "damage")

    apaczka = ApaczkaStub()
    stats = scan_zoho_damage_cases(
        client=UncorrelatedZohoStub(),
        shipping_store=shipping,
        damage_store=damage,
        apaczka_client=apaczka,
    )
    repeated = scan_zoho_damage_cases(
        client=UncorrelatedZohoStub(),
        shipping_store=shipping,
        damage_store=damage,
        apaczka_client=apaczka,
    )

    assert stats["provider_matches"] == 1
    assert repeated["provider_matches"] == 1
    assert apaczka.calls == 2
    case = damage.list_cases()[0]
    assert case["shipping_draft_id"] is None
    assert case["order_number"] == "1648"
    assert case["customer_name"] == "Jan Kowalski"
    assert case["customer_email"] == "jan@example.com"
    assert case["courier"] == "apaczka"
    assert case["apaczka_order_id"] == 991
    assert case["evidence"][0]["apaczka_service"] == "InPost Kurier"


def test_zoho_tracking_fetches_provider_data_and_links_existing_order(tmp_path):
    shipping = ShippingStore(local_root=tmp_path / "shipping")
    damage = DamageStore(local_root=tmp_path / "damage")
    matching = {
        **_allegro_draft(),
        "id": "shopify-1641",
        "source": "shopify",
        "shopify_order_number": "1641",
        "order_date": "2026-07-11T10:00:00Z",
        "customer_name": "Wojciech Religa",
        "receiver": {
            "first_name": "Wojciech",
            "last_name": "Religa",
            "email": "w.religa@post.pl",
            "phone": "600100200",
        },
        "shipping_address": {
            "street": "Kwiatowa",
            "building_number": "2",
            "city": "Warszawa",
            "post_code": "00-001",
        },
        "tracking_number": None,
        "courier": "apaczka",
    }
    unrelated = {
        **matching,
        "id": "shopify-other",
        "shopify_order_number": "1640",
        "customer_name": "Inny Klient",
        "receiver": {**matching["receiver"], "email": "other@example.com"},
    }
    shipping.upsert_draft(unrelated)
    shipping.upsert_draft(matching)
    stats = scan_zoho_damage_cases(
        client=ProviderLookupZohoStub(),
        shipping_store=shipping,
        damage_store=damage,
        inpost_client=InPostLookupStub(),
    )

    assert stats["inpost_matches"] == 1
    assert stats["errors"] == 0
    case = damage.list_cases()[0]
    assert case["shipping_draft_id"] == "shopify-1641"
    assert case["order_number"] == "1641"
    assert case["correlation_method"] == "inpost_tracking_lookup"
    assert set(case["correlation_matched_fields"]) >= {"email", "phone", "name"}
    assert case["inpost_shipment_id"] == 12345
    assert shipping.get_draft("shopify-1641")["tracking_number"] == case["tracking_number"]
