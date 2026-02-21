"""
zdrovena.month_closing.google_ads – Google Ads Invoice Downloader
===================================================================
Downloads monthly billing invoices via the Google Ads API (REST).
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

import requests

from zdrovena.month_closing.config import API_RETRY_COUNT, API_RETRY_DELAY, API_TIMEOUT, PDF_DOWNLOAD_DELAY

logger = logging.getLogger("zdrovena.month_closing.google_ads")


def _micros_to_decimal(micros: int | str | None) -> Decimal:
    if micros is None:
        return Decimal("0.00")
    return (Decimal(str(micros)) / Decimal("1000000")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class GoogleAdsInvoiceClient:
    BASE_URL = "https://googleads.googleapis.com/v18"
    TOKEN_URL = "https://oauth2.googleapis.com/token"

    def __init__(
        self,
        developer_token: str,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        customer_id: str,
        login_customer_id: str | None = None,
    ) -> None:
        self.developer_token = developer_token
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.customer_id = customer_id.replace("-", "").strip()
        self.login_customer_id = (
            login_customer_id.replace("-", "").strip() if login_customer_id else None
        )
        self.access_token: str | None = None
        self._session = requests.Session()

    def authenticate(self) -> None:
        resp = self._session.post(
            self.TOKEN_URL,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self.refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if "access_token" not in data:
            raise RuntimeError(f"Google OAuth token refresh failed: {data}")
        self.access_token = data["access_token"]

    def _headers(self) -> dict[str, str]:
        if not self.access_token:
            raise RuntimeError("Not authenticated.")
        h = {
            "Authorization": f"Bearer {self.access_token}",
            "developer-token": self.developer_token,
            "Content-Type": "application/json",
        }
        if self.login_customer_id:
            h["login-customer-id"] = self.login_customer_id
        return h

    def _api_post(self, endpoint: str, payload: dict) -> dict:
        url = f"{self.BASE_URL}/{endpoint}"
        delay = API_RETRY_DELAY
        last_exc: Exception | None = None
        for attempt in range(1, API_RETRY_COUNT + 1):
            try:
                resp = self._session.post(url, headers=self._headers(), json=payload, timeout=API_TIMEOUT)
                resp.raise_for_status()
                return resp.json()
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < API_RETRY_COUNT:
                    time.sleep(delay)
                    delay *= 2
        raise RuntimeError(f"Google Ads API failed after {API_RETRY_COUNT} attempts: {last_exc}")

    def _api_get(self, endpoint: str, params: dict | None = None) -> dict:
        url = f"{self.BASE_URL}/{endpoint}"
        delay = API_RETRY_DELAY
        last_exc: Exception | None = None
        for attempt in range(1, API_RETRY_COUNT + 1):
            try:
                resp = self._session.get(url, headers=self._headers(), params=params, timeout=API_TIMEOUT)
                resp.raise_for_status()
                return resp.json()
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < API_RETRY_COUNT:
                    time.sleep(delay)
                    delay *= 2
        raise RuntimeError(f"Google Ads API GET failed after {API_RETRY_COUNT} attempts: {last_exc}")

    def get_billing_setup_id(self) -> str:
        query = (
            "SELECT billing_setup.id, billing_setup.status "
            "FROM billing_setup WHERE billing_setup.status = 'APPROVED'"
        )
        data = self._api_post(f"customers/{self.customer_id}/googleAds:searchStream", {"query": query})
        results = []
        for batch in data if isinstance(data, list) else [data]:
            for row in batch.get("results", []):
                bs = row.get("billingSetup", {})
                bs_id = bs.get("id")
                if bs_id:
                    results.append(str(bs_id))
        if not results:
            raise RuntimeError(f"No approved billing setup found for customer {self.customer_id}.")
        return results[0]

    _MONTH_ENUM = {
        1: "JANUARY", 2: "FEBRUARY", 3: "MARCH", 4: "APRIL",
        5: "MAY", 6: "JUNE", 7: "JULY", 8: "AUGUST",
        9: "SEPTEMBER", 10: "OCTOBER", 11: "NOVEMBER", 12: "DECEMBER",
    }

    def list_invoices(self, year: int, month: int, billing_setup_id: str | None = None) -> list[dict[str, Any]]:
        if billing_setup_id is None:
            billing_setup_id = self.get_billing_setup_id()
        month_name = self._MONTH_ENUM.get(month)
        if not month_name:
            raise ValueError(f"Invalid month: {month}")
        billing_setup_rn = f"customers/{self.customer_id}/billingSetups/{billing_setup_id}"
        endpoint = (
            f"customers/{self.customer_id}/invoices:list"
            f"?billingSetup={billing_setup_rn}"
            f"&issueYear={year}&issueMonth={month_name}"
        )
        data = self._api_get(endpoint)
        raw_invoices = data.get("invoices", [])
        invoices = []
        for inv in raw_invoices:
            invoices.append({
                "id": inv.get("id", ""),
                "type": inv.get("type", ""),
                "issue_date": inv.get("issueDate", ""),
                "due_date": inv.get("dueDate", ""),
                "currency": inv.get("currencyCode", ""),
                "subtotal": _micros_to_decimal(inv.get("subtotalAmountMicros")),
                "tax": _micros_to_decimal(inv.get("taxAmountMicros")),
                "total": _micros_to_decimal(inv.get("totalAmountMicros")),
                "pdf_url": inv.get("pdfUrl", ""),
                "resource_name": inv.get("resourceName", ""),
            })
        return invoices

    def download_invoice_pdf(self, invoice: dict[str, Any], save_dir: Path) -> Path | None:
        pdf_url = invoice.get("pdf_url", "")
        if not pdf_url:
            return None
        inv_id = invoice.get("id", "unknown")
        issue_date = invoice.get("issue_date", "").replace("-", "")
        filename = f"GoogleAds_{issue_date}_{inv_id}.pdf"
        target = save_dir / filename
        if target.exists():
            return target
        delay = API_RETRY_DELAY
        last_exc: Exception | None = None
        for attempt in range(1, API_RETRY_COUNT + 1):
            try:
                resp = self._session.get(pdf_url, timeout=API_TIMEOUT)
                resp.raise_for_status()
                if not resp.content or len(resp.content) < 100:
                    raise RuntimeError(f"PDF content too small ({len(resp.content)} bytes)")
                save_dir.mkdir(parents=True, exist_ok=True)
                target.write_bytes(resp.content)
                return target
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < API_RETRY_COUNT:
                    time.sleep(delay)
                    delay *= 2
        return None

    def download_all_invoices(
        self, year: int, month: int, save_dir: Path, dry_run: bool = False
    ) -> list[dict[str, Any]]:
        invoices = self.list_invoices(year, month)
        if not invoices:
            return []
        if dry_run:
            return invoices
        save_dir.mkdir(parents=True, exist_ok=True)
        for inv in invoices:
            path = self.download_invoice_pdf(inv, save_dir)
            inv["pdf_path"] = str(path) if path else None
            time.sleep(PDF_DOWNLOAD_DELAY)
        return invoices
