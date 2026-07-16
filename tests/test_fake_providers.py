from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest
import requests
from uvicorn import Config, Server

from zdrovena.common.allegro import AllegroClient
from zdrovena.common.apaczka import ApaczkaClient
from zdrovena.common.client import FakturowniaClient as MonthCloseFakturowniaClient
from zdrovena.common.fakturownia import FakturowniaClient
from zdrovena.common.inpost import InPostClient
from zdrovena.common.shipping_exceptions import (
    ApaczkaBusinessError,
    FakturowniaBusinessError,
    InPostBusinessError,
)
from zdrovena.fake_providers.app import app


class _MemoryStorage:
    def __init__(self) -> None:
        self._files: dict[str, bytes] = {}

    def stream(self, key: str) -> Iterator[bytes]:
        if key not in self._files:
            raise FileNotFoundError(key)
        yield self._files[key]

    def upload_stream(self, stream: Any, key: str, _: str) -> None:
        self._files[key] = stream.read()


class _ExplodingAllegroTokenStore:
    def load_refresh_token(self) -> str | None:
        raise AssertionError("fake provider mode must not read persisted Allegro tokens")

    def save_refresh_token(self, token: str) -> bool:
        raise AssertionError(f"fake provider mode must not persist Allegro token: {token}")

    def load_access_token(self) -> tuple[str, float] | None:
        raise AssertionError("fake provider mode must not read persisted Allegro access tokens")

    def save_access_token(self, token: str, expires_at: float) -> bool:
        raise AssertionError(f"fake provider mode must not persist Allegro access token: {token}")


@pytest.fixture(scope="module")
def fake_provider_url() -> Iterator[str]:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    server = Server(Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            if requests.get(f"{base_url}/health", timeout=0.5).ok:
                break
        except requests.RequestException:
            time.sleep(0.05)
    else:
        server.should_exit = True
        raise RuntimeError("fake provider server did not start")

    yield base_url

    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(autouse=True)
def reset_fake_provider(fake_provider_url: str) -> Iterator[None]:
    response = requests.post(f"{fake_provider_url}/__fake__/reset", timeout=2)
    response.raise_for_status()
    yield


def _set_scenario(base_url: str, provider: str, operation: str, mode: str) -> None:
    response = requests.post(
        f"{base_url}/__fake__/scenario",
        json={"provider": provider, "operation": operation, "mode": mode},
        timeout=2,
    )
    response.raise_for_status()


def test_allegro_client_uses_fake_provider_over_http(
    fake_provider_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALLEGRO_BASE_URL", f"{fake_provider_url}/allegro")
    monkeypatch.setenv("ALLEGRO_AUTH_URL", f"{fake_provider_url}/allegro/auth/oauth/token")
    monkeypatch.setenv("PROVIDER_MODE", "fake")
    _set_scenario(fake_provider_url, "allegro", "create_command", "pending")

    client = AllegroClient(
        client_id="client-id",
        client_secret="client-secret",
        refresh_token="refresh-token",
        timeout=2,
        token_store=_ExplodingAllegroTokenStore(),
    )

    orders = client.list_orders()
    assert orders[0]["id"] == "fake-order-1"

    client.mark_order_processed("fake-order-1", "PROCESSING")
    command = client.create_ship_with_allegro_shipment(
        command_id="cmd-1",
        order_id="fake-order-1",
        credentials_id=None,
        sender={"name": "Zdrovena", "street": "Magazynowa 1"},
        receiver={"name": "Buyer", "street": "Prosta 1"},
        packages=[
            {
                "type": "PACKAGE",
                "length": {"value": 30, "unit": "CENTIMETER"},
                "width": {"value": 20, "unit": "CENTIMETER"},
                "height": {"value": 15, "unit": "CENTIMETER"},
                "weight": {"value": 1, "unit": "KILOGRAMS"},
            }
        ],
    )
    assert command["status"] == "IN_PROGRESS"

    status = client.get_ship_with_allegro_command_status("cmd-1")
    assert status["status"] == "SUCCESS"
    shipment = client.get_ship_with_allegro_shipment(status["shipmentId"])
    assert shipment["status"] == "CREATED"
    assert client.get_ship_with_allegro_label(status["shipmentId"]).startswith(b"%PDF")

    invoice = client.create_invoice_declaration(order_id="fake-order-1", invoice_number="FV/1/2026")
    client.upload_invoice_file(
        order_id="fake-order-1", invoice_id=invoice["id"], pdf_bytes=b"%PDF-1.4"
    )

    state = requests.get(f"{fake_provider_url}/__fake__/state", timeout=2).json()
    assert state["allegro"]["orders"]["fake-order-1"]["fulfillment"]["status"] == "PROCESSING"
    assert state["allegro"]["invoices"][invoice["id"]]["fileUploaded"] is True


def test_inpost_client_stateful_success_and_label_not_ready(
    fake_provider_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import zdrovena.common.inpost as inpost_module

    monkeypatch.setattr(inpost_module, "_BASE", f"{fake_provider_url}/inpost")
    client = InPostClient(api_token="fake-token", organization_id="org-1")

    shipment = client.create_kurier_shipment(
        receiver_first_name="Anna",
        receiver_last_name="Nowak",
        receiver_email="anna@example.test",
        receiver_phone="500500500",
        receiver_street="Prosta",
        receiver_building_number="1",
        receiver_city="Warszawa",
        receiver_post_code="00-001",
        sender={
            "name": "Zdrovena",
            "email": "sender@example.test",
            "phone": "500500501",
            "street": "Magazynowa",
            "building_number": "2",
            "city": "Warszawa",
            "post_code": "00-002",
        },
        reference="order-1576",
    )
    duplicate = client.create_kurier_shipment(
        receiver_first_name="Anna",
        receiver_last_name="Nowak",
        receiver_email="anna@example.test",
        receiver_phone="500500500",
        receiver_street="Prosta",
        receiver_building_number="1",
        receiver_city="Warszawa",
        receiver_post_code="00-001",
        sender={"name": "Zdrovena"},
        reference="order-1576",
    )
    assert duplicate["id"] == shipment["id"]

    dispatch = client.create_dispatch_order(shipment["id"], {"name": "Zdrovena"})
    assert dispatch["status"] == "created"
    assert client.get_label(shipment["id"]).startswith(b"%PDF")

    _set_scenario(fake_provider_url, "inpost", "get_label", "label_not_ready")
    with pytest.raises(InPostBusinessError):
        client.get_label(shipment["id"])


def test_apaczka_client_stateful_success_and_provider_validation_failure(
    fake_provider_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import zdrovena.common.apaczka as apaczka_module

    monkeypatch.setattr(apaczka_module, "_BASE", f"{fake_provider_url}/apaczka/api/v2")
    client = ApaczkaClient(
        app_id="app-id",
        app_secret="secret",
        service_id="21",
        storage=_MemoryStorage(),
    )

    shipment = client.create_shipment(
        receiver_name="Anna Nowak",
        receiver_firstname="Anna",
        receiver_lastname="Nowak",
        receiver_email="anna@example.test",
        receiver_phone="500500500",
        receiver_address="Prosta 1",
        receiver_city="Warszawa",
        receiver_zip="00-001",
        sender={"name": "Zdrovena"},
        reference="order-1639",
    )
    duplicate = client.create_shipment(
        receiver_name="Anna Nowak",
        receiver_firstname="Anna",
        receiver_lastname="Nowak",
        receiver_email="anna@example.test",
        receiver_phone="500500500",
        receiver_address="Prosta 1",
        receiver_city="Warszawa",
        receiver_zip="00-001",
        sender={"name": "Zdrovena"},
        reference="order-1639",
    )
    assert duplicate["id"] == shipment["id"]
    assert client.get_label("order-1639").startswith(b"%PDF")

    _set_scenario(fake_provider_url, "apaczka", "order_send", "provider_validation_failure")
    with pytest.raises(ApaczkaBusinessError):
        client.create_shipment(
            receiver_name="Anna Nowak",
            receiver_firstname="Anna",
            receiver_lastname="Nowak",
            receiver_email="anna@example.test",
            receiver_phone="500500500",
            receiver_address="Prosta 1",
            receiver_city="Warszawa",
            receiver_zip="00-001",
            sender={"name": "Zdrovena"},
            reference="order-validation-error",
        )


def test_fakturownia_client_stateful_success_and_existing_invoice(
    fake_provider_url: str,
) -> None:
    client = FakturowniaClient(
        base_url=f"{fake_provider_url}/fakturownia",
        api_token="fake-token",
        timeout=2,
    )

    invoice = client.create_invoice(
        {"kind": "vat", "number": "FV/1/2026", "oid": "order-1", "positions": []}
    )
    assert invoice["id"] == 1
    assert client.list_invoices(oid="order-1")[0]["number"] == "FV/1/2026"
    assert client.get_invoice_pdf(invoice["id"]).startswith(b"%PDF")

    with pytest.raises(FakturowniaBusinessError):
        client.create_invoice(
            {"kind": "vat", "number": "FV/2/2026", "oid": "order-1", "positions": []}
        )

    _set_scenario(fake_provider_url, "fakturownia", "create_invoice", "already_exists")
    duplicate = client.create_invoice(
        {"kind": "vat", "number": "FV/3/2026", "oid": "order-1", "positions": []}
    )
    assert duplicate["id"] == invoice["id"]


def test_month_close_fakturownia_client_uses_environment_base_url_and_filters(
    fake_provider_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = f"{fake_provider_url}/fakturownia/invoices.json?api_token=fake"
    for invoice in [
        {
            "number": "1/SMOKE",
            "oid": "smoke-sale",
            "income": "yes",
            "sell_date": "2026-06-15",
            "issue_date": "2026-06-15",
            "price_gross": "123.00",
        },
        {
            "number": "COST/SMOKE",
            "oid": "smoke-cost",
            "income": "no",
            "sell_date": "2026-06-16",
            "issue_date": "2026-06-16",
            "buyer_name": "Shopify",
            "price_gross": "49.00",
        },
        {
            "number": "OLD/SMOKE",
            "oid": "old-sale",
            "income": "yes",
            "sell_date": "2026-05-15",
            "issue_date": "2026-05-15",
            "price_gross": "10.00",
        },
    ]:
        response = requests.post(endpoint, json={"invoice": invoice}, timeout=2)
        response.raise_for_status()

    monkeypatch.setenv("FAKTUROWNIA_BASE_URL", f"{fake_provider_url}/fakturownia")
    monkeypatch.setenv("FAKTUROWNIA_API_TOKEN", "fake")
    client = MonthCloseFakturowniaClient.from_keyring(
        retry_count=1,
        retry_delay=0,
        timeout=2,
        pdf_delay=0,
    )

    sales = client.fetch_sales_invoices("2026-06-01", "2026-06-30")
    costs = client.fetch_cost_invoices("2026-06-01", "2026-06-30")

    assert [invoice["number"] for invoice in sales] == ["1/SMOKE"]
    assert [invoice["number"] for invoice in costs] == ["COST/SMOKE"]


def test_fake_provider_validates_contracts_and_can_reset_state(fake_provider_url: str) -> None:
    missing_auth = requests.post(
        f"{fake_provider_url}/inpost/v1/organizations/org-1/shipments",
        json={},
        timeout=2,
    )
    assert missing_auth.status_code == 401

    bad_body = requests.post(
        f"{fake_provider_url}/inpost/v1/organizations/org-1/shipments",
        headers={"Authorization": "Bearer fake-token"},
        json={"reference": "order-1"},
        timeout=2,
    )
    assert bad_body.status_code == 422

    created = requests.post(
        f"{fake_provider_url}/inpost/v1/organizations/org-1/shipments",
        headers={"Authorization": "Bearer fake-token"},
        json={
            "service": "inpost_courier_standard",
            "reference": "order-1",
            "receiver": {"email": "buyer@example.test"},
            "parcels": [{"template": "small"}],
        },
        timeout=2,
    )
    assert created.status_code == 200
    assert requests.get(f"{fake_provider_url}/__fake__/state", timeout=2).json()["inpost"][
        "shipments"
    ]

    requests.post(f"{fake_provider_url}/__fake__/reset", timeout=2).raise_for_status()
    assert not requests.get(f"{fake_provider_url}/__fake__/state", timeout=2).json()["inpost"][
        "shipments"
    ]
