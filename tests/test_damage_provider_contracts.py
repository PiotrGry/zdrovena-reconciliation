from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import requests
import responses as rsps

from zdrovena.common.allegro import AllegroClient
from zdrovena.common.apaczka import ApaczkaClient
from zdrovena.common.inpost import InPostClient
from zdrovena.month_closing.zoho_mail import ZohoMailClient


def test_allegro_tracking_uses_repeated_waybill_query_values():
    client = AllegroClient(client_id="cid", client_secret="secret", refresh_token="refresh")
    client._access_token = "access"
    client._expires_at = time.time() + 3600
    response = MagicMock(spec=requests.Response)
    response.status_code = 200
    response.ok = True
    response.json.return_value = {"carrierId": "ALLEGRO", "waybills": []}

    with patch.object(client._session, "request", return_value=response) as request:
        result = client.get_tracking_history("ALLEGRO", ["A001", "A002"])

    assert result["carrierId"] == "ALLEGRO"
    call = request.call_args
    assert call.args[:2] == ("GET", "https://api.allegro.pl/order/carriers/ALLEGRO/tracking")
    assert call.kwargs["params"] == {"waybill": ["A001", "A002"]}


def test_apaczka_lists_orders_with_bounded_pagination():
    client = ApaczkaClient("app-id", "secret", "", storage=MagicMock())
    with patch.object(
        client,
        "_call",
        return_value={"response": {"orders": [{"id": 991}]}},
    ) as call:
        orders = client.list_orders(page=2, limit=25)

    assert orders == [{"id": 991}]
    call.assert_called_once_with("orders", {"page": 2, "limit": 25})


@rsps.activate
def test_zoho_damage_search_reads_body_without_marking_message():
    api = "https://mail.zoho.eu/api"
    client = ZohoMailClient(
        client_id="cid",
        client_secret="secret",
        refresh_token="refresh",
        api_url=api,
    )
    client.access_token = "access"
    client.account_id = "account-1"
    message = {
        "messageId": "message-1",
        "folderId": "folder-1",
        "fromAddress": "uszkodzeniagda@inpost.pl",
        "subject": "Uszkodzona przesyłka",
        "receivedTime": 2000,
    }
    search_url = f"{api}/accounts/account-1/messages/search"
    rsps.add(rsps.GET, search_url, json={"data": [message]})
    for _ in range(3):
        rsps.add(rsps.GET, search_url, json={"data": []})
    rsps.add(
        rsps.GET,
        f"{api}/accounts/account-1/folders/folder-1/messages/message-1/content",
        json={"data": {"content": "<p>Paczka 123 została uszkodzona.</p>"}},
    )

    messages = client.search_damage_notifications(since_ms=1000)

    assert len(messages) == 1
    assert messages[0]["content"] == "Paczka 123 została uszkodzona."
    assert all(call.request.method == "GET" for call in rsps.calls)


@rsps.activate
def test_zoho_reads_configured_sender_aliases():
    api = "https://mail.zoho.eu/api"
    client = ZohoMailClient(
        client_id="cid",
        client_secret="secret",
        refresh_token="refresh",
        api_url=api,
    )
    client.access_token = "access"
    client.account_id = "account-1"
    rsps.add(
        rsps.GET,
        f"{api}/accounts/account-1",
        json={
            "data": {
                "emailAddress": [{"mailId": "piotr@wodahumio.pl", "isConfirmed": True}],
                "sendMailDetails": [{"fromAddress": "info@wodahumio.pl", "status": True}],
            }
        },
    )
    assert "info@wodahumio.pl" in client.sender_addresses()


def test_inpost_finds_organisation_shipment_by_exact_tracking():
    client = InPostClient("token", "org-1")
    response = MagicMock(spec=requests.Response)
    response.json.return_value = {
        "items": [
            {
                "id": 123,
                "tracking_number": "630015608680156036357414",
                "reference": "1641",
            }
        ]
    }
    with patch.object(client, "_request", return_value=response) as request:
        shipment = client.find_shipment_by_tracking("630015608680156036357414")

    assert shipment["reference"] == "1641"
    request.assert_called_once_with(
        "GET",
        "https://api-shipx-pl.easypack24.net/v1/organizations/org-1/shipments",
        action="find_shipment_by_tracking",
        params={"tracking_number": "630015608680156036357414", "per_page": 2},
    )
