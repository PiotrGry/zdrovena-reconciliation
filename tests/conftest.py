"""Shared fixtures for zdrovena tests."""

from __future__ import annotations

import pytest


@pytest.fixture()
def sample_invoice() -> dict:
    """A realistic sales invoice dict (as returned by Fakturownia API)."""
    return {
        "id": 101,
        "number": "5/02/2025",
        "kind": "vat",
        "sell_date": "2025-02-10",
        "issue_date": "2025-02-10",
        "warehouse_document_id": 201,
        "buyer_name": "Test Buyer",
        "positions": [
            {
                "name": "Woda Humio 500ml x 12 butelek",
                "quantity": 3,
            },
            {
                "name": "Woda Humio 500ml szkło x 6 butelek",
                "quantity": 1,
            },
            {
                "name": "Dostawa InPost",
                "quantity": 1,
            },
        ],
    }


@pytest.fixture()
def sample_receipt() -> dict:
    """A receipt (paragon) invoice."""
    return {
        "id": 102,
        "number": "P1/02/2025",
        "kind": "receipt",
        "sell_date": "2025-02-15",
        "issue_date": "2025-02-15",
        "positions": [
            {
                "name": "Zgrzewka wody Humio 500ml - 12 butelek",
                "quantity": 2,
            },
        ],
    }


@pytest.fixture()
def sample_wz_actions() -> dict[int, list[dict]]:
    """Warehouse actions grouped by document_id."""
    return {
        201: [
            {
                "warehouse_document_id": 201,
                "product_name": "Woda Humio butelka plastik",
                "quantity": "-36",
            },
            {
                "warehouse_document_id": 201,
                "product_name": "Woda Humio butelka szkło",
                "quantity": "-6",
            },
        ],
        202: [
            {
                "warehouse_document_id": 202,
                "product_name": "Woda Humio butelka plastik",
                "quantity": "-12",
            },
        ],
    }
