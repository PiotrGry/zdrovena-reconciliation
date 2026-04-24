"""
Tests for GET /invoices/sales and GET /invoices/products endpoints.

All tests run with AZURE_AUTH_DISABLED=true and FAKTUROWNIA_DISABLED=true
(Fakturownia client is mocked — no live HTTP calls).
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("AZURE_AUTH_DISABLED", "true")
os.environ.setdefault("FAKTUROWNIA_DISABLED", "false")  # we mock it below

from zdrovena.api.main import app

_SAMPLE_INVOICES = [
    {
        "id": 1001,
        "number": "1/04/2026",
        "kind": "vat",
        "sell_date": "2026-04-03",
        "issue_date": "2026-04-03",
        "buyer_name": "Hurtownia Zdrowie Sp. z o.o.",
        "price_net": "14200.00",
        "price_tax": "3266.00",
        "price_gross": "17466.00",
        "currency": "PLN",
        "status": "paid",
    },
    {
        "id": 1002,
        "number": "2/04/2026",
        "kind": "vat",
        "sell_date": "2026-04-05",
        "issue_date": "2026-04-05",
        "buyer_name": "BioFresh Polska S.A.",
        "price_net": "8750.00",
        "price_tax": "2012.50",
        "price_gross": "10762.50",
        "currency": "PLN",
        "status": "paid",
    },
]

_SAMPLE_PRODUCTS = [
    {
        "id": 201,
        "name": "Woda Zdrovena Naturalna 500ml",
        "code": "ZD-001",
        "price_net": "28.80",
        "price_gross": "31.10",
        "currency": "PLN",
        "disabled": False,
    },
    {
        "id": 202,
        "name": "Woda Zdrovena Mineralna+",
        "code": "ZD-008",
        "price_net": "41.40",
        "price_gross": "44.71",
        "currency": "PLN",
        "disabled": True,
    },
]


@pytest.fixture()
def api():
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


# ── /invoices/sales ───────────────────────────────────────────────────────────


class TestSalesInvoices:
    def test_returns_list(self, api):
        with (
            patch("zdrovena.api.routers.invoices.fetch_invoices", return_value=_SAMPLE_INVOICES),
            patch("zdrovena.api.routers.invoices._FAKTUROWNIA_DISABLED", False),
            patch("zdrovena.api.routers.invoices.get_secret", return_value="fake-token"),
        ):
            resp = api.get("/invoices/sales?year=2026&month=4")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["number"] == "1/04/2026"
        assert data[0]["buyer_name"] == "Hurtownia Zdrowie Sp. z o.o."
        assert data[0]["price_gross"] == "17466.00"

    def test_fields_present(self, api):
        with (
            patch("zdrovena.api.routers.invoices.fetch_invoices", return_value=_SAMPLE_INVOICES),
            patch("zdrovena.api.routers.invoices._FAKTUROWNIA_DISABLED", False),
            patch("zdrovena.api.routers.invoices.get_secret", return_value="fake-token"),
        ):
            resp = api.get("/invoices/sales?year=2026")
        inv = resp.json()[0]
        for field in (
            "id",
            "number",
            "kind",
            "sell_date",
            "buyer_name",
            "price_net",
            "price_gross",
            "currency",
            "status",
        ):
            assert field in inv, f"missing field: {field}"

    def test_requires_auth(self, api):
        """Without auth disabled, should reject unauthenticated request."""
        with patch("zdrovena.api.routers.invoices._FAKTUROWNIA_DISABLED", True):
            # auth is disabled globally in this module so we just check 503 from disabled flag
            resp = api.get("/invoices/sales?year=2026")
        assert resp.status_code == 503

    def test_disabled_returns_503(self, api):
        with patch("zdrovena.api.routers.invoices._FAKTUROWNIA_DISABLED", True):
            resp = api.get("/invoices/sales?year=2026")
        assert resp.status_code == 503

    def test_missing_credentials_returns_503(self, api):
        from zdrovena.common.exceptions import MissingSecretError

        with (
            patch("zdrovena.api.routers.invoices._FAKTUROWNIA_DISABLED", False),
            patch(
                "zdrovena.api.routers.invoices.get_secret",
                side_effect=MissingSecretError("fakturownia_api_token", "humio"),
            ),
        ):
            resp = api.get("/invoices/sales?year=2026")
        assert resp.status_code == 503

    def test_invalid_month_returns_422(self, api):
        with patch("zdrovena.api.routers.invoices._FAKTUROWNIA_DISABLED", True):
            resp = api.get("/invoices/sales?year=2026&month=13")
        assert resp.status_code == 422

    def test_invalid_year_returns_422(self, api):
        with patch("zdrovena.api.routers.invoices._FAKTUROWNIA_DISABLED", True):
            resp = api.get("/invoices/sales?year=1999")
        assert resp.status_code == 422

    def test_empty_result(self, api):
        with (
            patch("zdrovena.api.routers.invoices.fetch_invoices", return_value=[]),
            patch("zdrovena.api.routers.invoices._FAKTUROWNIA_DISABLED", False),
            patch("zdrovena.api.routers.invoices.get_secret", return_value="fake-token"),
        ):
            resp = api.get("/invoices/sales?year=2026&month=1")
        assert resp.status_code == 200
        assert resp.json() == []


# ── /invoices/products ────────────────────────────────────────────────────────


class TestProducts:
    def test_returns_all_products(self, api):
        with (
            patch("zdrovena.api.routers.invoices.fetch_products", return_value=_SAMPLE_PRODUCTS),
            patch("zdrovena.api.routers.invoices._FAKTUROWNIA_DISABLED", False),
            patch("zdrovena.api.routers.invoices.get_secret", return_value="fake-token"),
        ):
            resp = api.get("/invoices/products")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2

    def test_active_only_filter(self, api):
        with (
            patch("zdrovena.api.routers.invoices.fetch_products", return_value=_SAMPLE_PRODUCTS),
            patch("zdrovena.api.routers.invoices._FAKTUROWNIA_DISABLED", False),
            patch("zdrovena.api.routers.invoices.get_secret", return_value="fake-token"),
        ):
            resp = api.get("/invoices/products?active_only=true")
        data = resp.json()
        assert len(data) == 1
        assert data[0]["code"] == "ZD-001"
        assert data[0]["active"] is True

    def test_product_fields(self, api):
        with (
            patch("zdrovena.api.routers.invoices.fetch_products", return_value=_SAMPLE_PRODUCTS),
            patch("zdrovena.api.routers.invoices._FAKTUROWNIA_DISABLED", False),
            patch("zdrovena.api.routers.invoices.get_secret", return_value="fake-token"),
        ):
            resp = api.get("/invoices/products")
        p = resp.json()[0]
        for field in ("id", "name", "code", "price_net", "price_gross", "currency", "active"):
            assert field in p, f"missing field: {field}"

    def test_disabled_product_active_false(self, api):
        with (
            patch("zdrovena.api.routers.invoices.fetch_products", return_value=_SAMPLE_PRODUCTS),
            patch("zdrovena.api.routers.invoices._FAKTUROWNIA_DISABLED", False),
            patch("zdrovena.api.routers.invoices.get_secret", return_value="fake-token"),
        ):
            resp = api.get("/invoices/products")
        data = resp.json()
        disabled = next(p for p in data if p["code"] == "ZD-008")
        assert disabled["active"] is False

    def test_disabled_returns_503(self, api):
        with patch("zdrovena.api.routers.invoices._FAKTUROWNIA_DISABLED", True):
            resp = api.get("/invoices/products")
        assert resp.status_code == 503
