"""tests/test_fakturownia_client.py — TDD for FakturowniaClient.

Fakturownia REST API v1:
  Base:  https://<subdomain>.fakturownia.pl
  Auth:  api_token in query string OR JSON body
  Docs:  https://app.fakturownia.pl/api  +
         https://pomoc.fakturownia.pl/pola-przekazywane-z-programu-fakturownia-do-ksef-zgodnie-ze-schema-fa-3

`settlement_positions` field (KSeF `Rozliczenie`):
    Confirmed by official pomoc.fakturownia.pl mapping (2026-06-11).
    Structure inferred from Rails conventions + KSeF FA(3) schema:
        [{"kind": "charge"|"deduction", "amount": "<PLN>", "description": "<reason>"}]
    Adapter isolates subfield names — tests assert contract, not literal keys.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from zdrovena.common.fakturownia import FakturowniaClient
from zdrovena.common.shipping_exceptions import (
    CourierConnectionError,
    CourierTimeoutError,
    FakturowniaAuthError,
    FakturowniaBusinessError,
    FakturowniaServerError,
)

# ── helpers ────────────────────────────────────────────────────────────────────


def _resp(json_payload=None, status=200, text: str = ""):
    r = MagicMock(spec=requests.Response)
    r.ok = 200 <= status < 300
    r.status_code = status
    r.json.return_value = json_payload if json_payload is not None else {}
    r.text = text
    r.content = b""
    return r


@pytest.fixture
def client():
    return FakturowniaClient(
        base_url="https://zdrovena.fakturownia.pl",
        api_token="test-token-abc",
        timeout=15,
    )


# ── construction ─────────────────────────────────────────────────────────────


class TestConstruction:
    def test_base_url_and_token_stored(self, client):
        assert client.base_url == "https://zdrovena.fakturownia.pl"
        assert client.api_token == "test-token-abc"

    def test_base_url_strips_trailing_slash(self):
        c = FakturowniaClient(
            base_url="https://zdrovena.fakturownia.pl/",
            api_token="t",
        )
        assert c.base_url == "https://zdrovena.fakturownia.pl"

    def test_default_timeout(self):
        c = FakturowniaClient(base_url="https://x.fakturownia.pl", api_token="t")
        assert c.timeout == 30

    def test_custom_timeout(self, client):
        assert client.timeout == 15


# ── get_invoice ──────────────────────────────────────────────────────────────


class TestGetInvoice:
    def test_get_invoice_ok_returns_dict(self, client):
        payload = {"id": 12345, "number": "FV/2025/001", "positions": []}
        with patch("requests.Session.request", return_value=_resp(payload)) as mock:
            out = client.get_invoice(12345)
            assert out == payload
            _, kwargs = mock.call_args
            assert kwargs["method"] == "GET"
            assert "/invoices/12345.json" in kwargs["url"]
            assert kwargs["params"]["api_token"] == "test-token-abc"

    def test_get_invoice_404_raises_business_error(self, client):
        with patch("requests.Session.request", return_value=_resp({"code": "error"}, status=404)):
            with pytest.raises(FakturowniaBusinessError):
                client.get_invoice(999999)

    def test_get_invoice_401_raises_auth_error(self, client):
        with patch("requests.Session.request", return_value=_resp({"code": "unauthorized"}, status=401)):
            with pytest.raises(FakturowniaAuthError):
                client.get_invoice(12345)

    def test_get_invoice_500_raises_server_error(self, client):
        with patch("requests.Session.request", return_value=_resp({}, status=500)):
            with pytest.raises(FakturowniaServerError):
                client.get_invoice(12345)

    def test_get_invoice_timeout_raises_courier_timeout(self, client):
        with patch("requests.Session.request", side_effect=requests.Timeout("timed out")):
            with pytest.raises(CourierTimeoutError):
                client.get_invoice(12345)

    def test_get_invoice_connection_error(self, client):
        with patch("requests.Session.request", side_effect=requests.ConnectionError("dns fail")):
            with pytest.raises(CourierConnectionError):
                client.get_invoice(12345)


# ── update_invoice ───────────────────────────────────────────────────────────


class TestUpdateInvoice:
    def test_update_invoice_puts_wrapped_payload(self, client):
        with patch("requests.Session.request", return_value=_resp({"id": 111, "buyer_name": "New"})) as mock:
            client.update_invoice(111, {"buyer_name": "New"})
            _, kwargs = mock.call_args
            assert kwargs["method"] == "PUT"
            assert "/invoices/111.json" in kwargs["url"]
            body = kwargs["json"]
            # Must be wrapped: {"api_token": ..., "invoice": {...}}
            assert body["api_token"] == "test-token-abc"
            assert body["invoice"] == {"buyer_name": "New"}

    def test_update_invoice_returns_response_dict(self, client):
        payload = {"id": 111, "buyer_name": "New"}
        with patch("requests.Session.request", return_value=_resp(payload)):
            out = client.update_invoice(111, {"buyer_name": "New"})
            assert out == payload

    def test_update_invoice_422_raises_business_error(self, client):
        err = {"code": "error", "message": {"buyer_name": ["can't be blank"]}}
        with patch("requests.Session.request", return_value=_resp(err, status=422)):
            with pytest.raises(FakturowniaBusinessError):
                client.update_invoice(111, {"buyer_name": ""})


# ── add_settlement_position (KSeF Rozliczenie / kaucja) ─────────────────────


class TestAddSettlementPosition:
    def test_add_charge_builds_settlement_positions_payload(self, client):
        """Adds a `charge` (obciążenie) to invoice.

        Contract: sends PUT with `settlement_positions` containing
        {kind, amount, description}. Preserves any pre-existing settlements.
        """
        # Existing invoice has NO settlement_positions
        existing = {"id": 500, "number": "FV/2025/500", "settlement_positions": []}

        put_response = _resp({"id": 500, "settlement_positions": [{"id": 1}]})

        with patch("requests.Session.request") as mock:
            # 1st call: GET current invoice; 2nd call: PUT patch
            mock.side_effect = [_resp(existing), put_response]

            client.add_settlement_position(
                invoice_id=500,
                kind="charge",
                amount_pln="5.00",
                description="Kaucja za opakowania zwrotne",
            )

        # Inspect the PUT payload
        put_call = mock.call_args_list[1]
        assert put_call.kwargs["method"] == "PUT"
        body = put_call.kwargs["json"]
        assert body["api_token"] == "test-token-abc"
        settlements = body["invoice"]["settlement_positions"]
        assert isinstance(settlements, list)
        assert len(settlements) == 1
        row = settlements[0]
        assert row["kind"] == "charge"
        assert row["amount"] == "5.00"
        assert row["description"] == "Kaucja za opakowania zwrotne"

    def test_add_deduction_kind(self, client):
        existing = {"id": 600, "settlement_positions": []}
        with patch("requests.Session.request") as mock:
            mock.side_effect = [_resp(existing), _resp({"id": 600})]
            client.add_settlement_position(
                invoice_id=600,
                kind="deduction",
                amount_pln="10.00",
                description="Rabat",
            )
        row = mock.call_args_list[1].kwargs["json"]["invoice"]["settlement_positions"][0]
        assert row["kind"] == "deduction"

    def test_add_settlement_preserves_existing_rows(self, client):
        """When invoice already has settlements, new row is APPENDED not replaced."""
        existing = {
            "id": 700,
            "settlement_positions": [
                {"id": 42, "kind": "deduction", "amount": "2.00", "description": "Kompensata"},
            ],
        }
        with patch("requests.Session.request") as mock:
            mock.side_effect = [_resp(existing), _resp({"id": 700})]
            client.add_settlement_position(
                invoice_id=700,
                kind="charge",
                amount_pln="7.50",
                description="Kaucja za opakowania zwrotne",
            )
        settlements = mock.call_args_list[1].kwargs["json"]["invoice"]["settlement_positions"]
        assert len(settlements) == 2
        # existing row present with id (Rails "keep")
        assert any(s.get("id") == 42 for s in settlements)
        # new row appended
        assert any(
            s.get("kind") == "charge"
            and s.get("description") == "Kaucja za opakowania zwrotne"
            for s in settlements
        )

    def test_add_settlement_amount_decimal_conversion(self, client):
        """Amount accepted as float, str, or Decimal — always serialized as string with 2 decimals."""
        existing = {"id": 800, "settlement_positions": []}
        with patch("requests.Session.request") as mock:
            mock.side_effect = [_resp(existing), _resp({"id": 800})]
            client.add_settlement_position(
                invoice_id=800,
                kind="charge",
                amount_pln=5.5,  # float → "5.50"
                description="Test",
            )
        row = mock.call_args_list[1].kwargs["json"]["invoice"]["settlement_positions"][0]
        assert row["amount"] == "5.50"

    def test_add_settlement_rejects_zero_amount(self, client):
        with pytest.raises(ValueError, match="amount"):
            client.add_settlement_position(
                invoice_id=900,
                kind="charge",
                amount_pln="0",
                description="Test",
            )

    def test_add_settlement_rejects_negative_amount(self, client):
        with pytest.raises(ValueError, match="amount"):
            client.add_settlement_position(
                invoice_id=900,
                kind="charge",
                amount_pln="-1.00",
                description="Test",
            )

    def test_add_settlement_rejects_unknown_kind(self, client):
        with pytest.raises(ValueError, match="kind"):
            client.add_settlement_position(
                invoice_id=900,
                kind="bogus",
                amount_pln="1.00",
                description="Test",
            )

    def test_add_settlement_rejects_empty_description(self, client):
        with pytest.raises(ValueError, match="description"):
            client.add_settlement_position(
                invoice_id=900,
                kind="charge",
                amount_pln="1.00",
                description="",
            )

    def test_race_double_add_idempotent(self, client):
        """Race protection: jeśli między zewnętrznym idempotency check a naszym PUT-em
        inny worker dodał pozycję z tą samą description — nie robimy drugiego PUT-a.
        Zwracamy świeżo pobraną fakturę, PUT nie leci.
        """
        # Faktura już ma pozycję kaucji (dodaną przez równoległego workera)
        existing = {
            "id": 950,
            "settlement_positions": [
                {
                    "id": 77,
                    "kind": "charge",
                    "amount": "5.00",
                    "description": "Kaucja za opakowania zwrotne",
                }
            ],
        }

        with patch("requests.Session.request") as mock:
            # Tylko 1 request — GET. Żadnego PUT.
            mock.side_effect = [_resp(existing)]

            result = client.add_settlement_position(
                invoice_id=950,
                kind="charge",
                amount_pln="5.00",
                description="Kaucja za opakowania zwrotne",
            )

        # Dokładnie jeden call — GET, bez PUT-a
        assert mock.call_count == 1
        assert mock.call_args_list[0].kwargs["method"] == "GET"
        # Zwrócił istniejącą fakturę
        assert result == existing

    def test_race_double_add_case_insensitive_match(self, client):
        """Race check jest case-insensitive + stripped (jak has_settlement_with_description)."""
        existing = {
            "id": 951,
            "settlement_positions": [
                {"id": 1, "description": "  KAUCJA za opakowania zwrotne  "},
            ],
        }
        with patch("requests.Session.request") as mock:
            mock.side_effect = [_resp(existing)]
            client.add_settlement_position(
                invoice_id=951,
                kind="charge",
                amount_pln="5.00",
                description="Kaucja za opakowania zwrotne",
            )
        assert mock.call_count == 1  # tylko GET, bez PUT


# ── has_settlement_with_description (idempotency helper) ─────────────────────


class TestHasSettlementWithDescription:
    def test_returns_true_when_matching_description_exists(self):
        invoice = {
            "settlement_positions": [
                {"kind": "charge", "amount": "5.00", "description": "Kaucja za opakowania zwrotne"},
            ]
        }
        assert (
            FakturowniaClient.has_settlement_with_description(
                invoice, "Kaucja za opakowania zwrotne"
            )
            is True
        )

    def test_returns_false_when_no_match(self):
        invoice = {
            "settlement_positions": [
                {"kind": "deduction", "amount": "2.00", "description": "Kompensata"},
            ]
        }
        assert (
            FakturowniaClient.has_settlement_with_description(
                invoice, "Kaucja za opakowania zwrotne"
            )
            is False
        )

    def test_returns_false_when_no_settlements(self):
        assert (
            FakturowniaClient.has_settlement_with_description({}, "Kaucja") is False
        )
        assert (
            FakturowniaClient.has_settlement_with_description(
                {"settlement_positions": []}, "Kaucja"
            )
            is False
        )

    def test_match_is_case_insensitive_and_stripped(self):
        invoice = {
            "settlement_positions": [
                {"description": "  KAUCJA za opakowania zwrotne  "},
            ]
        }
        assert (
            FakturowniaClient.has_settlement_with_description(
                invoice, "Kaucja za opakowania zwrotne"
            )
            is True
        )


# ── list_invoices ────────────────────────────────────────────────────────────


class TestListInvoices:
    def test_list_invoices_passes_period_and_pagination(self, client):
        with patch("requests.Session.request", return_value=_resp([{"id": 1}, {"id": 2}])) as mock:
            out = client.list_invoices(period="this_month", page=2, per_page=50)
            _, kwargs = mock.call_args
            assert kwargs["method"] == "GET"
            assert "/invoices.json" in kwargs["url"]
            params = kwargs["params"]
            assert params["period"] == "this_month"
            assert params["page"] == 2
            assert params["per_page"] == 50
            assert params["api_token"] == "test-token-abc"
            assert out == [{"id": 1}, {"id": 2}]

    def test_list_invoices_by_number(self, client):
        with patch("requests.Session.request", return_value=_resp([{"id": 1, "number": "FV/2025/1"}])) as mock:
            client.list_invoices(number="FV/2025/1")
            params = mock.call_args.kwargs["params"]
            assert params["number"] == "FV/2025/1"

    def test_list_invoices_401_raises(self, client):
        with patch("requests.Session.request", return_value=_resp({}, status=401)):
            with pytest.raises(FakturowniaAuthError):
                client.list_invoices()


# ── error mapping edge cases ─────────────────────────────────────────────────


class TestErrorMapping:
    def test_403_raises_auth_error(self, client):
        with patch("requests.Session.request", return_value=_resp({}, status=403)):
            with pytest.raises(FakturowniaAuthError):
                client.get_invoice(1)

    def test_502_raises_server_error(self, client):
        with patch("requests.Session.request", return_value=_resp({}, status=502)):
            with pytest.raises(FakturowniaServerError):
                client.get_invoice(1)

    def test_400_raises_business_error(self, client):
        with patch("requests.Session.request", return_value=_resp({"error": "bad request"}, status=400)):
            with pytest.raises(FakturowniaBusinessError):
                client.get_invoice(1)


# ── from_env constructor ─────────────────────────────────────────────────────


class TestFromEnv:
    def test_from_env_reads_base_url_and_token(self, monkeypatch):
        monkeypatch.setenv("FAKTUROWNIA_BASE_URL", "https://custom.fakturownia.pl")
        monkeypatch.setenv("FAKTUROWNIA_API_TOKEN", "env-token-xyz")
        c = FakturowniaClient.from_env()
        assert c.base_url == "https://custom.fakturownia.pl"
        assert c.api_token == "env-token-xyz"

    def test_from_env_raises_when_token_missing(self, monkeypatch):
        monkeypatch.delenv("FAKTUROWNIA_API_TOKEN", raising=False)
        monkeypatch.setenv("FAKTUROWNIA_BASE_URL", "https://x.fakturownia.pl")
        with pytest.raises(RuntimeError, match="FAKTUROWNIA_API_TOKEN"):
            FakturowniaClient.from_env()
