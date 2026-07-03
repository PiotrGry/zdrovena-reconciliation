"""Tests for Ship with Allegro (Wysyłam z Allegro) methods on AllegroClient.

Endpoints covered:
- GET  /shipment-management/delivery-services
- GET  /shipment-management/delivery-proposals/{orderId}
- POST /shipment-management/shipments/create-commands
- GET  /shipment-management/shipments/create-commands/{commandId}
- GET  /shipment-management/shipments/{shipmentId}
- POST /shipment-management/pickup-proposals
- POST /shipment-management/pickups/create-commands
- GET  /shipment-management/shipments/{shipmentId}/label

Docs: https://developer.allegro.pl/tutorials/jak-zarzadzac-przesylkami-przez-wysylam-z-allegro-LRVjK7K21sY
"""

from __future__ import annotations

import base64
from unittest.mock import MagicMock, patch

import pytest

from zdrovena.common.allegro import AllegroClient, _normalize_pickup_proposals
from zdrovena.common.shipping_exceptions import AllegroBusinessError


def _mock_client() -> AllegroClient:
    c = AllegroClient(
        client_id="cid",
        client_secret="csec",
        refresh_token="rt",
        env="prod",
    )
    # Bypass OAuth
    c._access_token = "test-token"
    c._expires_at = 9999999999
    return c


def _mock_response(status: int = 200, json_data=None, content: bytes = b""):
    resp = MagicMock()
    resp.status_code = status
    resp.text = "" if json_data is None else str(json_data)
    resp.content = content
    resp.json.return_value = json_data if json_data is not None else {}
    return resp


# ── delivery-services ─────────────────────────────────────────────────────────


class TestGetDeliveryServices:
    def test_returns_service_list(self):
        c = _mock_client()
        services = [
            {
                "id": "svc-inpost-locker",
                "carrierId": "INPOST",
                "owner": "ALLEGRO",
                "name": "Allegro InPost Paczkomat",
                "additionalProperties": {"inpost#sendingMethod": {"required": False}},
            },
            {
                "id": "svc-dpd",
                "carrierId": "DPD",
                "owner": "ALLEGRO",
                "name": "Allegro DPD",
            },
        ]
        with patch.object(
            c._session,
            "request",
            return_value=_mock_response(200, {"deliveryServices": services}),
        ):
            result = c.get_delivery_services()
        assert result == services

    def test_empty_response_returns_empty_list(self):
        c = _mock_client()
        with patch.object(
            c._session,
            "request",
            return_value=_mock_response(200, {}),
        ):
            assert c.get_delivery_services() == []

    def test_uses_correct_endpoint(self):
        c = _mock_client()
        with patch.object(
            c._session,
            "request",
            return_value=_mock_response(200, {"deliveryServices": []}),
        ) as m:
            c.get_delivery_services()
        url = m.call_args[0][1]
        assert "/shipment-management/delivery-services" in url


# ── delivery-proposals ────────────────────────────────────────────────────────


class TestGetDeliveryProposal:
    def test_returns_proposal_for_order(self):
        c = _mock_client()
        proposal = {
            "deliveryMethodId": "svc-inpost-locker",
            "receiver": {"name": "Jan Nowak"},
            "packages": [{"dimensions": {"length": 30, "width": 20, "height": 15}}],
        }
        with patch.object(
            c._session,
            "request",
            return_value=_mock_response(200, proposal),
        ) as m:
            result = c.get_delivery_proposal("ORDER-123")
        assert result == proposal
        url = m.call_args[0][1]
        assert "/shipment-management/delivery-proposals/ORDER-123" in url


# ── create-commands (create shipment) ─────────────────────────────────────────


_SENDER = {
    "name": "Nadawca",
    "street": "Główna 30",
    "postalCode": "10-200",
    "city": "Wrocław",
    "countryCode": "PL",
    "email": "sender@mail.com",
    "phone": "500600700",
}
_RECEIVER = {
    "name": "Odbiorca",
    "street": "Testowa 1",
    "postalCode": "00-001",
    "city": "Wrocław",
    "countryCode": "PL",
    "email": "buyer@mail.com",
    "phone": "600700800",
}
_PACKAGE = {
    "type": "PACKAGE",
    "length": {"value": 30, "unit": "CENTIMETER"},
    "width": {"value": 20, "unit": "CENTIMETER"},
    "height": {"value": 15, "unit": "CENTIMETER"},
    "weight": {"value": 5.0, "unit": "KILOGRAMS"},
}


class TestCreateShipmentCommand:
    def test_posts_with_generated_command_id(self):
        c = _mock_client()
        with patch.object(
            c._session,
            "request",
            return_value=_mock_response(
                201,
                {"commandId": "cmd-uuid-1", "status": "IN_PROGRESS"},
            ),
        ) as m:
            result = c.create_ship_with_allegro_shipment(
                command_id="cmd-uuid-1",
                order_id="ORDER-123",
                delivery_method_id="svc-inpost-locker",
                credentials_id=None,
                sender=_SENDER,
                receiver={**_RECEIVER, "point": "WAW01A"},
                packages=[_PACKAGE],
            )
        assert result["commandId"] == "cmd-uuid-1"
        # Verify POST body follows the create-commands contract.
        _, kwargs = m.call_args
        body = kwargs["json"]
        assert body["commandId"] == "cmd-uuid-1"
        # order_id is sent as referenceNumber; there is no top-level orderId.
        assert body["input"]["referenceNumber"] == "ORDER-123"
        assert "orderId" not in body["input"]
        assert body["input"]["deliveryMethodId"] == "svc-inpost-locker"
        assert body["input"]["sender"] == _SENDER
        # Pickup-point code goes inside the receiver block as `point`.
        assert body["input"]["receiver"]["point"] == "WAW01A"
        assert "pickupPointId" not in body["input"]
        assert "credentialsId" not in body["input"] or body["input"]["credentialsId"] is None

    def test_omits_delivery_method_id_when_none(self):
        """Since 2026-07-01 deliveryMethodId is optional — Allegro derives it.

        Verify we do NOT send the field when not supplied. Future-proof against
        Q1 2027 removal of GET /shipment-management/delivery-services.
        """
        c = _mock_client()
        with patch.object(
            c._session,
            "request",
            return_value=_mock_response(201, {"commandId": "x", "status": "IN_PROGRESS"}),
        ) as m:
            c.create_ship_with_allegro_shipment(
                command_id="x",
                order_id="O1",
                credentials_id=None,
                sender=_SENDER,
                receiver=_RECEIVER,
                packages=[_PACKAGE],
            )
        body = m.call_args[1]["json"]
        assert "deliveryMethodId" not in body["input"]

    def test_still_sends_delivery_method_id_when_explicitly_set(self):
        """Callers with own agreements can still pin a specific method."""
        c = _mock_client()
        with patch.object(
            c._session,
            "request",
            return_value=_mock_response(201, {"commandId": "x", "status": "IN_PROGRESS"}),
        ) as m:
            c.create_ship_with_allegro_shipment(
                command_id="x",
                order_id="O1",
                delivery_method_id="own-agreement-method",
                credentials_id="creds-1",
                sender=_SENDER,
                receiver=_RECEIVER,
                packages=[_PACKAGE],
            )
        body = m.call_args[1]["json"]
        assert body["input"]["deliveryMethodId"] == "own-agreement-method"

    def test_additional_services_is_array_of_strings(self):
        c = _mock_client()
        with patch.object(
            c._session,
            "request",
            return_value=_mock_response(201, {"commandId": "x", "status": "IN_PROGRESS"}),
        ) as m:
            c.create_ship_with_allegro_shipment(
                command_id="x",
                order_id="O1",
                delivery_method_id="svc-inpost",
                credentials_id=None,
                sender=_SENDER,
                receiver=_RECEIVER,
                packages=[_PACKAGE],
                additional_services=["ADDITIONAL_HANDLING"],
            )
        body = m.call_args[1]["json"]
        assert body["input"]["additionalServices"] == ["ADDITIONAL_HANDLING"]

    def test_no_additional_services_key_when_omitted(self):
        c = _mock_client()
        with patch.object(
            c._session,
            "request",
            return_value=_mock_response(201, {"commandId": "x", "status": "IN_PROGRESS"}),
        ) as m:
            c.create_ship_with_allegro_shipment(
                command_id="x",
                order_id="O1",
                delivery_method_id="svc-inpost",
                credentials_id=None,
                sender=_SENDER,
                receiver=_RECEIVER,
                packages=[_PACKAGE],
            )
        body = m.call_args[1]["json"]
        assert "additionalServices" not in body["input"]

    def test_additional_properties_inpost_sending_method_sent(self):
        """P1-2: InPost sendingMethod goes to additionalProperties (issue #9915)."""
        c = _mock_client()
        with patch.object(
            c._session,
            "request",
            return_value=_mock_response(201, {"commandId": "x", "status": "IN_PROGRESS"}),
        ) as m:
            c.create_ship_with_allegro_shipment(
                command_id="x",
                order_id="O1",
                credentials_id=None,
                sender=_SENDER,
                receiver=_RECEIVER,
                packages=[_PACKAGE],
                additional_properties={"inpost#sendingMethod": "parcel_locker"},
            )
        body = m.call_args[1]["json"]
        assert body["input"]["additionalProperties"] == {"inpost#sendingMethod": "parcel_locker"}

    def test_additional_properties_omitted_by_default(self):
        """P1-2: additionalProperties key must be absent when not provided."""
        c = _mock_client()
        with patch.object(
            c._session,
            "request",
            return_value=_mock_response(201, {"commandId": "x", "status": "IN_PROGRESS"}),
        ) as m:
            c.create_ship_with_allegro_shipment(
                command_id="x",
                order_id="O1",
                credentials_id=None,
                sender=_SENDER,
                receiver=_RECEIVER,
                packages=[_PACKAGE],
            )
        body = m.call_args[1]["json"]
        assert "additionalProperties" not in body["input"]

    def test_additional_properties_empty_dict_treated_as_omitted(self):
        """Empty dict should not produce a payload key."""
        c = _mock_client()
        with patch.object(
            c._session,
            "request",
            return_value=_mock_response(201, {"commandId": "x", "status": "IN_PROGRESS"}),
        ) as m:
            c.create_ship_with_allegro_shipment(
                command_id="x",
                order_id="O1",
                credentials_id=None,
                sender=_SENDER,
                receiver=_RECEIVER,
                packages=[_PACKAGE],
                additional_properties={},
            )
        body = m.call_args[1]["json"]
        assert "additionalProperties" not in body["input"]

    def test_own_agreement_passes_credentials_id(self):
        c = _mock_client()
        with patch.object(
            c._session,
            "request",
            return_value=_mock_response(201, {"commandId": "x", "status": "IN_PROGRESS"}),
        ) as m:
            c.create_ship_with_allegro_shipment(
                command_id="x",
                order_id="O1",
                delivery_method_id="svc-dpd",
                credentials_id="cred-abc",
                sender=_SENDER,
                receiver=_RECEIVER,
                packages=[_PACKAGE],
            )
        body = m.call_args[1]["json"]
        assert body["input"]["credentialsId"] == "cred-abc"

    def test_package_uses_flat_dims_and_plural_weight_unit(self):
        c = _mock_client()
        with patch.object(
            c._session,
            "request",
            return_value=_mock_response(201, {"commandId": "x", "status": "IN_PROGRESS"}),
        ) as m:
            c.create_ship_with_allegro_shipment(
                command_id="x",
                order_id="O1",
                delivery_method_id="svc",
                credentials_id=None,
                sender=_SENDER,
                receiver=_RECEIVER,
                packages=[_PACKAGE],
            )
        pkg = m.call_args[1]["json"]["input"]["packages"][0]
        assert pkg["type"] == "PACKAGE"
        # FLAT dims (no `dimensions` wrapper) using `.value`.
        assert "dimensions" not in pkg
        assert pkg["length"]["value"] == 30
        assert pkg["weight"]["value"] == 5.0
        assert pkg["weight"]["unit"] == "KILOGRAMS"

    def test_business_error_raises(self):
        c = _mock_client()
        with patch.object(
            c._session,
            "request",
            return_value=_mock_response(400, {"error": "bad order"}),
        ):
            with pytest.raises(AllegroBusinessError):
                c.create_ship_with_allegro_shipment(
                    command_id="x",
                    order_id="O1",
                    delivery_method_id="svc",
                    credentials_id=None,
                    sender=_SENDER,
                    receiver=_RECEIVER,
                    packages=[_PACKAGE],
                )


# ── polling create-commands status ────────────────────────────────────────────


class TestGetCreateShipmentStatus:
    def test_returns_success_status(self):
        c = _mock_client()
        payload = {
            "commandId": "cmd-1",
            "status": "SUCCESS",
            "shipmentId": "ship-42",
        }
        with patch.object(
            c._session,
            "request",
            return_value=_mock_response(200, payload),
        ) as m:
            result = c.get_ship_with_allegro_command_status("cmd-1")
        assert result == payload
        url = m.call_args[0][1]
        assert "/shipment-management/shipments/create-commands/cmd-1" in url

    def test_returns_error_status_with_details(self):
        c = _mock_client()
        payload = {
            "commandId": "cmd-1",
            "status": "ERROR",
            "shipmentId": None,
            "errors": [{"code": "DELIVERY_METHOD_NOT_AVAILABLE", "message": "..."}],
        }
        with patch.object(
            c._session,
            "request",
            return_value=_mock_response(200, payload),
        ):
            result = c.get_ship_with_allegro_command_status("cmd-1")
        assert result["status"] == "ERROR"
        assert result["errors"][0]["code"] == "DELIVERY_METHOD_NOT_AVAILABLE"


class TestWaitForShipment:
    """Helper that polls create-command until SUCCESS or ERROR."""

    def test_returns_shipment_id_on_success(self):
        c = _mock_client()
        responses = [
            _mock_response(200, {"status": "IN_PROGRESS"}),
            _mock_response(200, {"status": "IN_PROGRESS"}),
            _mock_response(200, {"status": "SUCCESS", "shipmentId": "ship-99"}),
        ]
        with patch.object(
            c._session,
            "request",
            side_effect=responses,
        ):
            with patch("time.sleep"):
                result = c.wait_for_ship_with_allegro_shipment(
                    "cmd-1", max_attempts=5, interval_s=0
                )
        assert result == "ship-99"

    def test_raises_on_error_status(self):
        c = _mock_client()
        with patch.object(
            c._session,
            "request",
            return_value=_mock_response(
                200,
                {"status": "ERROR", "errors": [{"code": "X", "message": "boom"}]},
            ),
        ):
            with pytest.raises(AllegroBusinessError):
                c.wait_for_ship_with_allegro_shipment("cmd-1", max_attempts=3, interval_s=0)

    def test_raises_on_timeout(self):
        """Timeout → AllegroCommandPending (subklasa AllegroBusinessError, backward compat).

        Wołający powinien odróżniać pending od twardego ERROR przez typ wyjątku,
        nie przez substring "timed out" w message.
        """
        from zdrovena.common.shipping_exceptions import AllegroCommandPending

        c = _mock_client()
        with patch.object(
            c._session,
            "request",
            return_value=_mock_response(200, {"status": "IN_PROGRESS"}),
        ):
            with patch("time.sleep"):
                with pytest.raises(AllegroCommandPending) as excinfo:
                    c.wait_for_ship_with_allegro_shipment("cmd-1", max_attempts=3, interval_s=0)
                assert excinfo.value.command_id == "cmd-1"
                # Backward compat: nadal jest AllegroBusinessError
                assert isinstance(excinfo.value, AllegroBusinessError)


# ── get shipment details ──────────────────────────────────────────────────────


class TestGetShipmentDetails:
    def test_returns_transporting_info_new_field(self):
        c = _mock_client()
        payload = {
            "id": "ship-99",
            "packages": [
                {
                    "id": "pkg-1",
                    "transportingInfo": [{"carrierId": "INPOST", "carrierWaybill": "6200XYZ"}],
                    # Deprecated field kept during transition until 2026-07-01
                    "waybill": "6200XYZ",
                }
            ],
        }
        with patch.object(
            c._session,
            "request",
            return_value=_mock_response(200, payload),
        ) as m:
            shipment = c.get_ship_with_allegro_shipment("ship-99")
        assert shipment == payload
        url = m.call_args[0][1]
        assert "/shipment-management/shipments/ship-99" in url

    def test_extract_first_waybill_helper(self):
        c = _mock_client()
        shipment = {
            "packages": [{"transportingInfo": [{"carrierId": "DPD", "carrierWaybill": "WAY-1"}]}]
        }
        assert c.extract_shipment_waybill(shipment) == ("DPD", "WAY-1")

    def test_extract_waybill_none_when_empty_string(self):
        c = _mock_client()
        shipment = {"packages": [{"transportingInfo": [{"carrierId": "X", "carrierWaybill": ""}]}]}
        assert c.extract_shipment_waybill(shipment) == ("X", None)

    def test_extract_waybill_none_when_no_packages(self):
        c = _mock_client()
        assert c.extract_shipment_waybill({"packages": []}) == (None, None)


# ── pickup proposals + pickups/create-commands ────────────────────────────────


class TestPickupProposals:
    def test_posts_shipment_ids_legacy_flat(self):
        """Older sandbox/mock shape: top-level proposalItems (deprecated)."""
        c = _mock_client()
        with patch.object(
            c._session,
            "request",
            return_value=_mock_response(
                200,
                {
                    "proposalItems": [
                        {
                            "id": "prop-1",
                            "pickupDate": "2026-07-02",
                            "timeSlot": {"from": "10:00", "to": "14:00"},
                        }
                    ]
                },
            ),
        ) as m:
            proposals = c.get_ship_with_allegro_pickup_proposals(["ship-99"])
        assert proposals[0]["id"] == "prop-1"
        body = m.call_args[1]["json"]
        assert body["input"]["shipmentIds"] == ["ship-99"]

    def test_parses_new_nested_pickup_times(self):
        """Post-2026-07-01 shape: nested proposals[].pickupTimes[]."""
        c = _mock_client()
        payload = [
            {
                "proposals": [
                    {
                        "shipmentId": "ba88f0fb-acf3-438a-877e-580da50c0874",
                        "pickupTimes": [
                            {
                                "date": "2026-07-05",
                                "minTime": "08:00",
                                "maxTime": "12:00",
                            },
                            {
                                "date": "2026-07-06",
                                "minTime": "10:00",
                                "maxTime": "14:00",
                            },
                        ],
                    }
                ],
                "address": {"street": "Foo 1"},
            }
        ]
        with patch.object(
            c._session,
            "request",
            return_value=_mock_response(200, payload),
        ):
            proposals = c.get_ship_with_allegro_pickup_proposals(["ship-99"])
        assert len(proposals) == 2
        assert proposals[0]["date"] == "2026-07-05"
        assert proposals[0]["minTime"] == "08:00"
        assert proposals[0]["shipmentId"] == "ba88f0fb-acf3-438a-877e-580da50c0874"
        # No legacy id should be synthesized
        assert "id" not in proposals[0]

    def test_parses_legacy_nested_proposal_items(self):
        """Older prod shape (still supported by Allegro until end of June 2026)."""
        c = _mock_client()
        payload = [
            {
                "proposals": [
                    {
                        "shipmentId": "ship-99",
                        "proposalItems": [
                            {
                                "id": "2023071210001300",
                                "name": "2023-07-12 10:00-13:00",
                            }
                        ],
                    }
                ],
                "address": {},
            }
        ]
        with patch.object(c._session, "request", return_value=_mock_response(200, payload)):
            proposals = c.get_ship_with_allegro_pickup_proposals(["ship-99"])
        assert len(proposals) == 1
        assert proposals[0]["id"] == "2023071210001300"

    def test_parses_mixed_shape_prefers_pickup_times(self):
        """Server returning both new and legacy shapes side-by-side."""
        payload = [
            {
                "proposals": [
                    {
                        "shipmentId": "s",
                        "pickupTimes": [
                            {"date": "2026-07-05", "minTime": "08:00", "maxTime": "12:00"}
                        ],
                        "proposalItems": [{"id": "legacy-id", "name": "legacy"}],
                    }
                ]
            }
        ]
        proposals = _normalize_pickup_proposals(payload)
        # Both surfaces returned; caller picks by presence of `date` (new) first.
        assert any(p.get("date") == "2026-07-05" for p in proposals)
        assert any(p.get("id") == "legacy-id" for p in proposals)

    def test_normalize_handles_empty_and_malformed_input(self):
        assert _normalize_pickup_proposals(None) == []
        assert _normalize_pickup_proposals({}) == []
        assert _normalize_pickup_proposals([]) == []
        assert _normalize_pickup_proposals({"unrelated": "payload"}) == []
        assert _normalize_pickup_proposals("garbage") == []


class TestCreatePickupCommand:
    def test_posts_pickup_command_new_format(self):
        """New-format pickupTime (post-2026-07-01) is sent as-is."""
        c = _mock_client()
        with patch.object(
            c._session,
            "request",
            return_value=_mock_response(
                201,
                {"commandId": "pu-cmd-1", "status": "IN_PROGRESS"},
            ),
        ) as m:
            result = c.create_ship_with_allegro_pickup(
                command_id="pu-cmd-1",
                shipment_ids=["ship-99"],
                pickup_time={
                    "date": "2026-07-05",
                    "minTime": "08:00",
                    "maxTime": "12:00",
                },
            )
        assert result["commandId"] == "pu-cmd-1"
        body = m.call_args[1]["json"]
        assert body["commandId"] == "pu-cmd-1"
        assert body["input"]["pickupTime"] == {
            "date": "2026-07-05",
            "minTime": "08:00",
            "maxTime": "12:00",
        }
        assert body["input"]["shipmentIds"] == ["ship-99"]
        # New-format must NOT include legacy field.
        assert "pickupDateProposalId" not in body["input"]
        assert "proposalItemId" not in body["input"]

    def test_posts_pickup_command_legacy_format(self):
        """Legacy proposal_item_id maps to the deprecated pickupDateProposalId.

        Kept working for sandbox / pre-2026-07-01 servers that still accept it.
        """
        c = _mock_client()
        with patch.object(
            c._session,
            "request",
            return_value=_mock_response(
                201,
                {"commandId": "pu-cmd-1", "status": "IN_PROGRESS"},
            ),
        ) as m:
            result = c.create_ship_with_allegro_pickup(
                command_id="pu-cmd-1",
                proposal_item_id="prop-1",
                shipment_ids=["ship-99"],
            )
        assert result["commandId"] == "pu-cmd-1"
        body = m.call_args[1]["json"]
        assert body["commandId"] == "pu-cmd-1"
        assert body["input"]["pickupDateProposalId"] == "prop-1"
        assert body["input"]["shipmentIds"] == ["ship-99"]

    def test_rejects_missing_pickup_selector(self):
        c = _mock_client()
        with pytest.raises(ValueError, match="pickup_time"):
            c.create_ship_with_allegro_pickup(
                command_id="pu-cmd-1",
                shipment_ids=["ship-99"],
            )

    def test_pickup_time_wins_over_proposal_id_when_both(self):
        c = _mock_client()
        with patch.object(
            c._session,
            "request",
            return_value=_mock_response(201, {"commandId": "x"}),
        ) as m:
            c.create_ship_with_allegro_pickup(
                command_id="x",
                shipment_ids=["s"],
                pickup_time={"date": "2026-07-05", "minTime": "08:00", "maxTime": "12:00"},
                proposal_item_id="legacy-should-be-ignored",
            )
        body = m.call_args[1]["json"]
        assert "pickupTime" in body["input"]
        assert "pickupDateProposalId" not in body["input"]


# ── cancel shipment / dispatch ────────────────────────────────────────────────


class TestCancelShipment:
    def test_posts_cancel_shipment_command(self):
        c = _mock_client()
        with patch.object(
            c._session,
            "request",
            return_value=_mock_response(201, {"commandId": "cxl-1", "status": "IN_PROGRESS"}),
        ) as m:
            result = c.cancel_ship_with_allegro_shipment(command_id="cxl-1", shipment_id="ship-42")
        assert result["commandId"] == "cxl-1"
        url = m.call_args[0][1]
        assert "/shipment-management/shipments/cancel-commands" in url
        body = m.call_args[1]["json"]
        assert body["commandId"] == "cxl-1"
        assert body["input"] == {"shipmentId": "ship-42"}

    def test_business_error_raises(self):
        c = _mock_client()
        with patch.object(
            c._session,
            "request",
            return_value=_mock_response(400, {"error": "already dispatched"}),
        ):
            with pytest.raises(AllegroBusinessError):
                c.cancel_ship_with_allegro_shipment(command_id="cxl-1", shipment_id="ship-42")


class TestCancelDispatch:
    def test_posts_cancel_dispatch_command(self):
        c = _mock_client()
        with patch.object(
            c._session,
            "request",
            return_value=_mock_response(201, {"commandId": "cxl-2", "status": "IN_PROGRESS"}),
        ) as m:
            result = c.cancel_ship_with_allegro_dispatch(command_id="cxl-2", dispatch_id="disp-9")
        assert result["commandId"] == "cxl-2"
        url = m.call_args[0][1]
        assert "/shipment-management/dispatches/cancel-commands" in url
        body = m.call_args[1]["json"]
        assert body["commandId"] == "cxl-2"
        assert body["input"] == {"dispatchId": "disp-9"}

    def test_business_error_raises(self):
        c = _mock_client()
        with patch.object(
            c._session,
            "request",
            return_value=_mock_response(400, {"error": "already accepted"}),
        ):
            with pytest.raises(AllegroBusinessError):
                c.cancel_ship_with_allegro_dispatch(command_id="cxl-2", dispatch_id="disp-9")


# ── label ─────────────────────────────────────────────────────────────────────


class TestGetShipmentLabel:
    def test_returns_pdf_bytes(self):
        c = _mock_client()
        pdf = b"%PDF-1.4 fake label"
        resp = _mock_response(200, content=pdf)
        resp.content = pdf
        with patch.object(
            c._session,
            "request",
            return_value=resp,
        ) as m:
            data = c.get_ship_with_allegro_label("ship-99")
        assert data == pdf
        url = m.call_args[0][1]
        assert "/shipment-management/shipments/ship-99/label" in url

    def test_accepts_base64_response(self):
        c = _mock_client()
        raw = b"%PDF-1.4 label"
        b64 = base64.b64encode(raw).decode()
        with patch.object(
            c._session,
            "request",
            return_value=_mock_response(200, {"label": b64}),
        ):
            data = c.get_ship_with_allegro_label("ship-99")
        assert data == raw
