"""
Tests for ZohoMailClient HTTP layer (responses mock) + validate_invoice_dates strict=False.

Note: zoho_mail.py is excluded from coverage measurement (requires live Zoho credentials),
but the business logic is verified here using the `responses` HTTP mock library.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import responses as rsps

from zdrovena.month_closing.zoho_mail import ZohoMailClient

OAUTH_URL = "https://accounts.zoho.eu/oauth/v2/token"
API_BASE = "https://mail.zoho.eu/api"
ACCOUNT_ID = "ACC123"


def _ts_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _april_2026_ts(day: int = 22) -> int:
    return _ts_ms(datetime(2026, 4, day, 12, 0, 0, tzinfo=timezone.utc))


def _setup_auth() -> None:
    rsps.add(rsps.POST, OAUTH_URL, json={"access_token": "TOKEN123", "expires_in": 3600})
    rsps.add(rsps.GET, f"{API_BASE}/accounts", json={"data": [{"accountId": ACCOUNT_ID}]})


class TestZohoVendorDownload:
    def _client(self) -> ZohoMailClient:
        return ZohoMailClient(
            client_id="cid",
            client_secret="csec",
            refresh_token="rtoken",
            api_url=API_BASE,
            accounts_url=OAUTH_URL,
        )

    @rsps.activate
    def test_download_attachment_saved(self, tmp_path: Path) -> None:
        """Happy path: 1 email with PDF attachment → file saved, found=True, downloaded=1."""
        _setup_auth()
        rsps.add(
            rsps.GET,
            f"{API_BASE}/accounts/{ACCOUNT_ID}/messages/search",
            json={
                "data": [
                    {
                        "messageId": "MSG1",
                        "folderId": "FOLDER1",
                        "fromAddress": "billing@shopify.com",
                        "hasAttachment": "1",
                        "receivedTime": _april_2026_ts(22),
                    }
                ]
            },
        )
        rsps.add(
            rsps.GET,
            f"{API_BASE}/accounts/{ACCOUNT_ID}/folders/FOLDER1/messages/MSG1",
            json={"data": {"attachments": [{"attachmentName": "HUMIO__519851974.pdf", "attachmentId": "ATT1"}]}},
        )
        rsps.add(
            rsps.GET,
            f"{API_BASE}/accounts/{ACCOUNT_ID}/folders/FOLDER1/messages/MSG1/attachments/ATT1",
            body=b"%PDF-1.0 fake invoice content",
        )

        client = self._client()
        client.authenticate()
        result = client.search_and_download_vendor(
            vendor_name="Shopify",
            search_term="billing@shopify.com",
            date_from="2026/04/01",
            date_to="2026/04/30",
            save_dir=tmp_path,
        )

        assert result["found"] is True
        assert result["downloaded"] == 1
        saved = list(tmp_path.glob("*.pdf"))
        assert len(saved) == 1
        assert "tmp" not in saved[0].name.lower()

    @rsps.activate
    def test_no_messages_returns_not_found(self, tmp_path: Path) -> None:
        """Empty search result → found=False, no files on disk."""
        _setup_auth()
        rsps.add(
            rsps.GET,
            f"{API_BASE}/accounts/{ACCOUNT_ID}/messages/search",
            json={"data": []},
        )

        client = self._client()
        client.authenticate()
        result = client.search_and_download_vendor(
            vendor_name="Shopify",
            search_term="billing@shopify.com",
            date_from="2026/04/01",
            date_to="2026/04/30",
            save_dir=tmp_path,
        )

        assert result["found"] is False
        assert result["downloaded"] == 0
        assert not list(tmp_path.glob("*.pdf"))

    @rsps.activate
    def test_message_outside_date_range_skipped(self, tmp_path: Path) -> None:
        """Email with receivedTime outside the requested range is filtered client-side."""
        _setup_auth()
        march_ts = _ts_ms(datetime(2026, 3, 15, 12, 0, 0, tzinfo=timezone.utc))
        rsps.add(
            rsps.GET,
            f"{API_BASE}/accounts/{ACCOUNT_ID}/messages/search",
            json={
                "data": [
                    {
                        "messageId": "MSG_OLD",
                        "folderId": "FOLDER1",
                        "fromAddress": "billing@shopify.com",
                        "hasAttachment": "1",
                        "receivedTime": march_ts,
                    }
                ]
            },
        )

        client = self._client()
        client.authenticate()
        result = client.search_and_download_vendor(
            vendor_name="Shopify",
            search_term="billing@shopify.com",
            date_from="2026/04/01",
            date_to="2026/04/30",
            save_dir=tmp_path,
        )

        assert result["found"] is False
        assert not list(tmp_path.glob("*.pdf"))


class TestInvoiceDateCheckStrictFalse:
    """validate_invoice_dates strict=False: unreadable PDF goes to accepted, not unverified."""

    def _unreadable_pdf(self, tmp_path: Path) -> Path:
        p = tmp_path / "invoice.pdf"
        p.write_bytes(b"not a real pdf")
        return p

    def test_strict_false_accepts_unreadable_pdf(self, tmp_path: Path) -> None:
        from zdrovena.month_closing.invoice_date_check import validate_invoice_dates

        pdf = self._unreadable_pdf(tmp_path)
        accepted, rejected, unverified = validate_invoice_dates(
            [pdf],
            month_start=date(2026, 4, 1),
            month_end=date(2026, 4, 30),
            strict=False,
        )
        assert pdf in accepted
        assert pdf not in unverified
        assert pdf not in rejected

    def test_strict_true_puts_unreadable_pdf_in_unverified(self, tmp_path: Path) -> None:
        from zdrovena.month_closing.invoice_date_check import validate_invoice_dates

        pdf = self._unreadable_pdf(tmp_path)
        accepted, rejected, unverified = validate_invoice_dates(
            [pdf],
            month_start=date(2026, 4, 1),
            month_end=date(2026, 4, 30),
            strict=True,
        )
        assert pdf in unverified
        assert pdf not in accepted
        assert pdf not in rejected
