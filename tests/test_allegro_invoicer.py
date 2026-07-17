"""Tests for zdrovena.api.routers.allegro_invoicer.create_invoice_for_order.

Flow: map order -> Fakturownia invoice, skip if invoice not required or
already exists (oid lookup), else create + fetch PDF + push to Allegro.
On any failure: log ERROR and send exactly one SMS alert.
"""

from __future__ import annotations

import logging
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
    def test_still_creates_invoice_when_not_required(self):
        """Buyer opted for a receipt/paragon instead of a VAT invoice —
        this business still issues a Fakturownia invoice for the sale,
        addressed to the buyer as a private individual."""
        fakturownia = MagicMock()
        fakturownia.list_invoices.return_value = []
        fakturownia.create_invoice.return_value = {"id": 1, "number": "1/07/2026"}
        fakturownia.get_invoice_pdf.return_value = b"%PDF-1.4"
        allegro = MagicMock()
        allegro.create_invoice_declaration.return_value = {"id": "decl-1"}
        order = _order(invoice={"required": False, "address": None})
        result = create_invoice_for_order(
            order, fakturownia_client=fakturownia, allegro_client=allegro
        )
        assert result["status"] == "created"
        fakturownia.create_invoice.assert_called_once()
        created_payload = fakturownia.create_invoice.call_args.args[0]
        assert created_payload["buyer_first_name"] == "Anna"
        assert created_payload["buyer_company"] == "0"


class TestIdempotency:
    def test_skips_when_invoice_already_exists_for_order(self):
        fakturownia = MagicMock()
        fakturownia.list_invoices.return_value = [{"id": 1, "number": "FV/2026/1", "oid": "af1"}]
        allegro = MagicMock()
        result = create_invoice_for_order(
            _order(), fakturownia_client=fakturownia, allegro_client=allegro
        )
        assert result["status"] == "already_exists"
        # Must recover the existing invoice's id/number so the caller can
        # persist it instead of clearing state and looping (the 502 bug).
        assert result["fakturownia_invoice_id"] == 1
        assert result["fakturownia_invoice_number"] == "FV/2026/1"
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

    def test_settlement_position_not_sent_inline_but_added_via_followup_call(self):
        """P0 regression guard: Fakturownia's POST /invoices.json rejects an
        inline settlement_positions block (422 "Nieprawidłowy atrybut:
        'description'", confirmed against the live API) — kaucja must be
        added via a separate add_settlement_position() PUT after creation,
        not embedded in the create_invoice() call."""
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
        create_call_body = fakturownia.create_invoice.call_args.args[0]
        assert "settlement_positions" not in create_call_body

        fakturownia.add_settlement_position.assert_called_once_with(
            invoice_id=999,
            kind="charge",
            amount_pln="6.00",
            description="Kaucja za opakowania zwrotne",
        )

    def test_settlement_and_full_payment_are_applied_before_pdf_is_fetched(self):
        """The PDF pushed to Allegro must include the kaucja and show the
        complete post-settlement total as paid."""
        call_order: list[str] = []
        fakturownia = MagicMock()
        fakturownia.list_invoices.return_value = []
        fakturownia.create_invoice.return_value = {"id": 999, "number": "FV/2026/999"}
        fakturownia.add_settlement_position.side_effect = lambda **kw: call_order.append(
            "add_settlement_position"
        )
        fakturownia.change_invoice_status.side_effect = lambda *a: call_order.append(
            "change_invoice_status"
        )
        fakturownia.get_invoice_pdf.side_effect = lambda *a: (
            call_order.append("get_invoice_pdf") or b"%PDF-1.4 fake"
        )
        allegro = MagicMock()
        allegro.create_invoice_declaration.return_value = {"id": "alg-inv-1"}

        create_invoice_for_order(_order(), fakturownia_client=fakturownia, allegro_client=allegro)

        assert call_order == [
            "add_settlement_position",
            "change_invoice_status",
            "get_invoice_pdf",
        ]
        fakturownia.change_invoice_status.assert_called_once_with(999, "paid")

    def test_no_settlement_call_when_order_has_no_deposit(self):
        fakturownia = MagicMock()
        fakturownia.list_invoices.return_value = []
        fakturownia.create_invoice.return_value = {"id": 999, "number": "FV/2026/999"}
        fakturownia.get_invoice_pdf.return_value = b"%PDF-1.4 fake"
        allegro = MagicMock()
        allegro.create_invoice_declaration.return_value = {"id": "alg-inv-1"}

        order = _order()
        order["lineItems"][0].pop("deposit")
        result = create_invoice_for_order(
            order, fakturownia_client=fakturownia, allegro_client=allegro
        )

        assert result["status"] == "created"
        fakturownia.add_settlement_position.assert_not_called()
        fakturownia.change_invoice_status.assert_called_once_with(999, "paid")

    def test_settlement_position_failure_logs_and_alerts_invoice_already_created(self):
        fakturownia = MagicMock()
        fakturownia.list_invoices.return_value = []
        fakturownia.create_invoice.return_value = {"id": 999, "number": "FV/2026/999"}
        fakturownia.add_settlement_position.side_effect = RuntimeError("Fakturownia 422")
        allegro = MagicMock()

        with patch("zdrovena.api.routers.allegro_invoicer._alert_invoice_failure") as mock_alert:
            result = create_invoice_for_order(
                _order(), fakturownia_client=fakturownia, allegro_client=allegro
            )

        assert result["status"] == "error"
        assert result["fakturownia_invoice_id"] == 999
        assert "Fakturownia 422" in result["error"]
        mock_alert.assert_called_once()
        fakturownia.get_invoice_pdf.assert_not_called()
        allegro.create_invoice_declaration.assert_not_called()

    def test_paid_status_failure_preserves_invoice_and_does_not_push_unpaid_pdf(self):
        fakturownia = MagicMock()
        fakturownia.list_invoices.return_value = []
        fakturownia.create_invoice.return_value = {"id": 999, "number": "FV/2026/999"}
        fakturownia.change_invoice_status.side_effect = RuntimeError("Fakturownia status 500")
        allegro = MagicMock()

        with patch("zdrovena.api.routers.allegro_invoicer._alert_invoice_failure") as mock_alert:
            result = create_invoice_for_order(
                _order(), fakturownia_client=fakturownia, allegro_client=allegro
            )

        assert result == {
            "status": "error",
            "error": "Fakturownia status 500",
            "fakturownia_invoice_id": 999,
        }
        mock_alert.assert_called_once()
        fakturownia.get_invoice_pdf.assert_not_called()
        allegro.create_invoice_declaration.assert_not_called()


class TestFailureAlerts:
    def test_fakturownia_create_failure_logs_and_alerts(self, monkeypatch):
        monkeypatch.setenv("SMSAPI_TOKEN_FOR_TEST", "unused")
        fakturownia = MagicMock()
        fakturownia.list_invoices.return_value = []
        fakturownia.create_invoice.side_effect = RuntimeError("Fakturownia 500")
        allegro = MagicMock()

        with patch("zdrovena.api.routers.allegro_invoicer._alert_invoice_failure") as mock_alert:
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

        with patch("zdrovena.api.routers.allegro_invoicer._alert_invoice_failure") as mock_alert:
            result = create_invoice_for_order(
                _order(), fakturownia_client=fakturownia, allegro_client=allegro
            )

        assert result["status"] == "error"
        assert result["fakturownia_invoice_id"] == 999
        mock_alert.assert_called_once()


# ── totalToPay sanity check ──────────────────────────────────────────────────


def _order_with_summary(*, total_to_pay: str, delivery_cost: str = "0.00", **overrides) -> dict:
    base = _order(**overrides)
    base["summary"] = {"totalToPay": {"amount": total_to_pay, "currency": "PLN"}}
    base["delivery"] = {"cost": {"amount": delivery_cost, "currency": "PLN"}}
    return base


class TestTotalSanityCheck:
    def test_no_warning_when_totals_match(self, caplog):
        """Default fixture: quantity=1, price 73.00, deposit 6.00,
        totalToPay 79.00, no delivery cost. (73+6)*1 = 79 — matches.
        """
        fakturownia = MagicMock()
        fakturownia.list_invoices.return_value = []
        fakturownia.create_invoice.return_value = {"id": 999, "number": "FV/2026/999"}
        fakturownia.get_invoice_pdf.return_value = b"%PDF-1.4 fake"
        allegro = MagicMock()
        allegro.create_invoice_declaration.return_value = {"id": "alg-inv-1"}

        order = _order_with_summary(total_to_pay="79.00")
        with caplog.at_level(logging.WARNING, logger="zdrovena.api.routers.allegro_invoicer"):
            create_invoice_for_order(order, fakturownia_client=fakturownia, allegro_client=allegro)

        assert not any("mismatch" in r.message.lower() for r in caplog.records)

    def test_warns_on_mismatch(self, caplog):
        fakturownia = MagicMock()
        fakturownia.list_invoices.return_value = []
        fakturownia.create_invoice.return_value = {"id": 999, "number": "FV/2026/999"}
        fakturownia.get_invoice_pdf.return_value = b"%PDF-1.4 fake"
        allegro = MagicMock()
        allegro.create_invoice_declaration.return_value = {"id": "alg-inv-1"}

        # Wrong totalToPay — 200.00 does not match (73+6)*1 = 79.00
        order = _order_with_summary(total_to_pay="200.00")
        with caplog.at_level(logging.WARNING, logger="zdrovena.api.routers.allegro_invoicer"):
            result = create_invoice_for_order(
                order, fakturownia_client=fakturownia, allegro_client=allegro
            )

        assert result["status"] == "created"  # mismatch does NOT block creation
        assert any("mismatch" in r.message.lower() for r in caplog.records)
        assert any(str(order["id"]) in r.message for r in caplog.records)

    def test_accounts_for_delivery_cost(self, caplog):
        """totalToPay includes delivery cost, which isn't part of the invoice
        — the check must subtract it before comparing, not treat it as a
        mismatch.
        """
        fakturownia = MagicMock()
        fakturownia.list_invoices.return_value = []
        fakturownia.create_invoice.return_value = {"id": 999, "number": "FV/2026/999"}
        fakturownia.get_invoice_pdf.return_value = b"%PDF-1.4 fake"
        allegro = MagicMock()
        allegro.create_invoice_declaration.return_value = {"id": "alg-inv-1"}

        # (73+6)*1 = 79.00 invoice total, + 12.50 delivery = 91.50 totalToPay
        order = _order_with_summary(total_to_pay="91.50", delivery_cost="12.50")
        with caplog.at_level(logging.WARNING, logger="zdrovena.api.routers.allegro_invoicer"):
            create_invoice_for_order(order, fakturownia_client=fakturownia, allegro_client=allegro)

        assert not any("mismatch" in r.message.lower() for r in caplog.records)

    def test_missing_summary_skips_check_silently(self, caplog):
        """No summary.totalToPay at all (e.g. an older/different order shape)
        — must not crash and must not warn, since there's nothing to compare.
        """
        fakturownia = MagicMock()
        fakturownia.list_invoices.return_value = []
        fakturownia.create_invoice.return_value = {"id": 999, "number": "FV/2026/999"}
        fakturownia.get_invoice_pdf.return_value = b"%PDF-1.4 fake"
        allegro = MagicMock()
        allegro.create_invoice_declaration.return_value = {"id": "alg-inv-1"}

        order = _order()  # no "summary" key at all
        with caplog.at_level(logging.WARNING, logger="zdrovena.api.routers.allegro_invoicer"):
            result = create_invoice_for_order(
                order, fakturownia_client=fakturownia, allegro_client=allegro
            )

        assert result["status"] == "created"
        assert not any("mismatch" in r.message.lower() for r in caplog.records)

    def test_malformed_summary_shape_does_not_raise(self, caplog):
        """order.summary is present but not dict-shaped (e.g. a list) — must
        not raise AttributeError, must not warn (nothing meaningful to
        compare), and invoice creation must still succeed.
        """
        fakturownia = MagicMock()
        fakturownia.list_invoices.return_value = []
        fakturownia.create_invoice.return_value = {"id": 999, "number": "FV/2026/999"}
        fakturownia.get_invoice_pdf.return_value = b"%PDF-1.4 fake"
        allegro = MagicMock()
        allegro.create_invoice_declaration.return_value = {"id": "alg-inv-1"}

        order = _order()
        order["summary"] = ["unexpected", "shape"]  # malformed: not a dict
        with caplog.at_level(logging.WARNING, logger="zdrovena.api.routers.allegro_invoicer"):
            result = create_invoice_for_order(
                order, fakturownia_client=fakturownia, allegro_client=allegro
            )

        assert result["status"] == "created"
        assert not any("mismatch" in r.message.lower() for r in caplog.records)

    def test_unparseable_total_does_not_raise(self, caplog):
        """totalToPay.amount is present but not a valid number — must not
        raise, must not warn, invoice creation must still succeed.
        """
        fakturownia = MagicMock()
        fakturownia.list_invoices.return_value = []
        fakturownia.create_invoice.return_value = {"id": 999, "number": "FV/2026/999"}
        fakturownia.get_invoice_pdf.return_value = b"%PDF-1.4 fake"
        allegro = MagicMock()
        allegro.create_invoice_declaration.return_value = {"id": "alg-inv-1"}

        order = _order_with_summary(total_to_pay="not-a-number")
        with caplog.at_level(logging.WARNING, logger="zdrovena.api.routers.allegro_invoicer"):
            result = create_invoice_for_order(
                order, fakturownia_client=fakturownia, allegro_client=allegro
            )

        assert result["status"] == "created"
        assert not any("mismatch" in r.message.lower() for r in caplog.records)


class TestRecoveryResume:
    """R4.1: recovering an existing invoice must RESUME the incomplete steps
    (settlement / PDF / Allegro push) idempotently, never create a second
    invoice, and never duplicate the Allegro declaration."""

    def test_partial_failure_then_retry_resumes_allegro_push(self):
        # Call 1: invoice created in Fakturownia, but the Allegro push fails.
        fakturownia = MagicMock()
        fakturownia.list_invoices.return_value = []
        fakturownia.create_invoice.return_value = {"id": 999, "number": "FV/2026/999"}
        fakturownia.get_invoice_pdf.return_value = b"%PDF"
        allegro = MagicMock()
        allegro.create_invoice_declaration.side_effect = RuntimeError("Allegro 503")
        with patch("zdrovena.api.routers.allegro_invoicer._alert_invoice_failure"):
            first = create_invoice_for_order(
                _order(), fakturownia_client=fakturownia, allegro_client=allegro
            )
        assert first["status"] == "error"
        # The Fakturownia invoice id is preserved so the retry can resume it.
        assert first["fakturownia_invoice_id"] == 999

        # Call 2 (retry): Fakturownia now reports the invoice exists; the order
        # has NOT been pushed to Allegro yet → resume must complete the push.
        fakturownia2 = MagicMock()
        fakturownia2.list_invoices.return_value = [
            {"id": 999, "number": "FV/2026/999", "oid": "af1"}
        ]
        fakturownia2.get_invoice_pdf.return_value = b"%PDF"
        allegro2 = MagicMock()
        allegro2.list_order_invoices.return_value = []  # not pushed yet
        allegro2.create_invoice_declaration.return_value = {"id": "alg-1"}
        second = create_invoice_for_order(
            _order(), fakturownia_client=fakturownia2, allegro_client=allegro2
        )
        assert second["status"] == "already_exists"
        assert second["fakturownia_invoice_id"] == 999
        # No second Fakturownia invoice.
        fakturownia2.create_invoice.assert_not_called()
        # Retry reapplies the final paid status idempotently after confirming
        # the settlement row, so a previously partial invoice is repaired too.
        fakturownia2.change_invoice_status.assert_called_once_with(999, "paid")
        # The push was resumed.
        allegro2.create_invoice_declaration.assert_called_once_with(
            order_id="af1", invoice_number="FV/2026/999"
        )
        allegro2.upload_invoice_file.assert_called_once()

    def test_partial_declaration_then_retry_reuploads_to_existing(self):
        # THE key scenario (#133): declaration was created but the PDF upload
        # failed. On retry we must re-upload to the SAME declaration id — never
        # create a second declaration.
        fakturownia = MagicMock()
        fakturownia.list_invoices.return_value = [
            {"id": 999, "number": "FV/2026/999", "oid": "af1"}
        ]
        fakturownia.get_invoice_pdf.return_value = b"%PDF"
        allegro = MagicMock()
        # A declaration for OUR invoice number already exists (id alg-decl-7).
        allegro.list_order_invoices.return_value = [
            {"id": "alg-decl-7", "invoiceNumber": "FV/2026/999", "fileType": "VAT"}
        ]
        result = create_invoice_for_order(
            _order(), fakturownia_client=fakturownia, allegro_client=allegro
        )
        assert result["status"] == "already_exists"
        assert result["fakturownia_invoice_id"] == 999
        # No second Fakturownia invoice, no second Allegro declaration.
        fakturownia.create_invoice.assert_not_called()
        allegro.create_invoice_declaration.assert_not_called()
        # PDF re-uploaded to the EXISTING declaration id.
        allegro.upload_invoice_file.assert_called_once_with(
            order_id="af1", invoice_id="alg-decl-7", pdf_bytes=b"%PDF"
        )

    def test_declaration_for_different_invoice_is_not_matched(self):
        # A declaration for a DIFFERENT invoice number must not count as ours —
        # we must create our own declaration, not hijack the unrelated one.
        fakturownia = MagicMock()
        fakturownia.list_invoices.return_value = [
            {"id": 999, "number": "FV/2026/999", "oid": "af1"}
        ]
        fakturownia.get_invoice_pdf.return_value = b"%PDF"
        allegro = MagicMock()
        allegro.list_order_invoices.return_value = [
            {"id": "alg-other", "invoiceNumber": "FV/2026/000", "fileType": "VAT"}
        ]
        allegro.create_invoice_declaration.return_value = {"id": "alg-mine"}
        result = create_invoice_for_order(
            _order(), fakturownia_client=fakturownia, allegro_client=allegro
        )
        assert result["status"] == "already_exists"
        allegro.create_invoice_declaration.assert_called_once_with(
            order_id="af1", invoice_number="FV/2026/999"
        )
        allegro.upload_invoice_file.assert_called_once_with(
            order_id="af1", invoice_id="alg-mine", pdf_bytes=b"%PDF"
        )

    def test_two_repeated_retries_do_not_duplicate_declaration(self):
        # Repeated retries with a matching declaration re-upload (idempotent PUT)
        # but never create a second declaration or invoice.
        for _ in range(2):
            fakturownia = MagicMock()
            fakturownia.list_invoices.return_value = [
                {"id": 999, "number": "FV/2026/999", "oid": "af1"}
            ]
            fakturownia.get_invoice_pdf.return_value = b"%PDF"
            allegro = MagicMock()
            allegro.list_order_invoices.return_value = [
                {"id": "alg-decl-7", "invoiceNumber": "FV/2026/999"}
            ]
            result = create_invoice_for_order(
                _order(), fakturownia_client=fakturownia, allegro_client=allegro
            )
            assert result["status"] == "already_exists"
            assert result["fakturownia_invoice_id"] == 999
            fakturownia.create_invoice.assert_not_called()
            allegro.create_invoice_declaration.assert_not_called()
            allegro.upload_invoice_file.assert_called_once_with(
                order_id="af1", invoice_id="alg-decl-7", pdf_bytes=b"%PDF"
            )

    def test_resume_settlement_failure_preserves_id_for_next_retry(self):
        # Existing invoice, but re-attaching the settlement fails → must return
        # error WITH the invoice id preserved so a further retry resumes again.
        fakturownia = MagicMock()
        fakturownia.list_invoices.return_value = [
            {"id": 999, "number": "FV/2026/999", "oid": "af1"}
        ]
        fakturownia.add_settlement_position.side_effect = RuntimeError("Fakturownia 500")
        allegro = MagicMock()
        with patch("zdrovena.api.routers.allegro_invoicer._alert_invoice_failure"):
            result = create_invoice_for_order(
                _order(), fakturownia_client=fakturownia, allegro_client=allegro
            )
        assert result["status"] == "error"
        assert result["fakturownia_invoice_id"] == 999
        fakturownia.create_invoice.assert_not_called()
