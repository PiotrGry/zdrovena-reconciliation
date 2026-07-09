"""Tests for zdrovena.common.sms_service."""

from __future__ import annotations

from unittest.mock import patch

from zdrovena.common.sms_service import send_invoice_failure_sms


class TestSendInvoiceFailureSms:
    def test_sends_sms_with_order_and_reason(self):
        with patch("httpx.post") as mock_post:
            mock_post.return_value.raise_for_status.return_value = None
            send_invoice_failure_sms(
                notify_phone="+48601000000",
                allegro_order_id="af1",
                reason="Fakturownia 500",
                token="tok",
            )
        _, kwargs = mock_post.call_args
        assert "af1" in kwargs["data"]["message"]
        assert "Fakturownia 500" in kwargs["data"]["message"]

    def test_empty_phone_is_noop(self):
        with patch("httpx.post") as mock_post:
            send_invoice_failure_sms(
                notify_phone="", allegro_order_id="af1", reason="x", token="tok"
            )
        mock_post.assert_not_called()
