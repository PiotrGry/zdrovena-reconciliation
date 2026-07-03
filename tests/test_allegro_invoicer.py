"""Tests for zdrovena.api.routers.allegro_invoicer.create_invoice_for_order.

Flow: map order -> Fakturownia invoice, skip if invoice not required or
already exists (oid lookup), else create + fetch PDF + push to Allegro.
On any failure: log ERROR and send exactly one SMS alert.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from zdrovena.api.routers.allegro_invoicer import create_invoice_for_order


def _order(**overrides) -> dict:
    base = {
        "id": "af1",
        "buyer": {"email": "b@example.com", "firstName": "Anna", "lastName": "Nowak"},
        "invoice": {"required": True, "address": None},
        "lineItems": [
            {
                "offer": {"name": "HUMIO 6 PET"},
                "quantity": 1,
                "price": {"amount": "73.00", "currency": "PLN"},
                "tax": {"rate": "8.00"},
                "deposit": {"price": {"amount": "6.00"}},
            }
        ],
    }
    base.update(overrides)
    return base


class TestNotRequired:
    def test_skips_when_invoice_not_required(self):
        fakturownia = MagicMock()
        allegro = MagicMock()
        order = _order(invoice={"required": False, "address": None})
        result = create_invoice_for_order(
            order, fakturownia_client=fakturownia, allegro_client=allegro
        )
        assert result["status"] == "not_required"
        fakturownia.create_invoice.assert_not_called()
        allegro.create_invoice_declaration.assert_not_called()


class TestIdempotency:
    def test_skips_when_invoice_already_exists_for_order(self):
        fakturownia = MagicMock()
        fakturownia.list_invoices.return_value = [{"id": 1, "oid": "af1"}]
        allegro = MagicMock()
        result = create_invoice_for_order(
            _order(), fakturownia_client=fakturownia, allegro_client=allegro
        )
        assert result["status"] == "already_exists"
        fakturownia.create_invoice.assert_not_called()
        fakturownia.list_invoices.assert_called_once_with(oid="af1")


class TestSuccessPath:
    def test_creates_invoice_fetches_pdf_and_pushes_to_allegro(self):
        fakturownia = MagicMock()
        fakturownia.list_invoices.return_value = []
        fakturownia.create_invoice.return_value = {"id": 999, "number": "FV/2026/999"}
        fakturownia.get_invoice_pdf.return_value = b"%PDF-1.4 fake"
        allegro = MagicMock()
        allegro.create_invoice_declaration.return_value = {"id": "alg-inv-1"}

        result = create_invoice_for_order(
            _order(), fakturownia_client=fakturownia, allegro_client=allegro
        )

        assert result["status"] == "created"
        assert result["fakturownia_invoice_id"] == 999
        fakturownia.get_invoice_pdf.assert_called_once_with(999)
        allegro.create_invoice_declaration.assert_called_once_with(
            order_id="af1", invoice_number="FV/2026/999"
        )
        allegro.upload_invoice_file.assert_called_once_with(
            order_id="af1", invoice_id="alg-inv-1", pdf_bytes=b"%PDF-1.4 fake"
        )


class TestFailureAlerts:
    def test_fakturownia_create_failure_logs_and_alerts(self, monkeypatch):
        monkeypatch.setenv("SMSAPI_TOKEN_FOR_TEST", "unused")
        fakturownia = MagicMock()
        fakturownia.list_invoices.return_value = []
        fakturownia.create_invoice.side_effect = RuntimeError("Fakturownia 500")
        allegro = MagicMock()

        with patch(
            "zdrovena.api.routers.allegro_invoicer._alert_invoice_failure"
        ) as mock_alert:
            result = create_invoice_for_order(
                _order(), fakturownia_client=fakturownia, allegro_client=allegro
            )

        assert result["status"] == "error"
        assert "Fakturownia 500" in result["error"]
        assert "fakturownia_invoice_id" not in result
        mock_alert.assert_called_once()
        assert mock_alert.call_args.kwargs["allegro_order_id"] == "af1"
        allegro.create_invoice_declaration.assert_not_called()

    def test_allegro_push_failure_logs_and_alerts_but_invoice_already_created(self):
        """If Fakturownia succeeded but the Allegro push fails, the invoice
        still exists in Fakturownia (oid-based idempotency will find it next
        time) — we must not lose that fact, just alert that the push failed.
        """
        fakturownia = MagicMock()
        fakturownia.list_invoices.return_value = []
        fakturownia.create_invoice.return_value = {"id": 999, "number": "FV/2026/999"}
        fakturownia.get_invoice_pdf.return_value = b"%PDF-1.4 fake"
        allegro = MagicMock()
        allegro.create_invoice_declaration.side_effect = RuntimeError("Allegro 502")

        with patch(
            "zdrovena.api.routers.allegro_invoicer._alert_invoice_failure"
        ) as mock_alert:
            result = create_invoice_for_order(
                _order(), fakturownia_client=fakturownia, allegro_client=allegro
            )

        assert result["status"] == "error"
        assert result["fakturownia_invoice_id"] == 999
        mock_alert.assert_called_once()
