"""
zdrovena.common.client – Fakturownia REST API Client
======================================================
Provides:
  • Keychain-based authentication (macOS Keychain via ``keyring``)
  • Paginated invoice fetching (sales & cost)
  • PDF downloading (single & batch)
  • Generic GET with retry + exponential backoff
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import keyring
import requests

from zdrovena.common.config import (
    DEFAULT_DOMAIN,
    DEFAULT_PDF_DELAY,
    DEFAULT_PER_PAGE,
    DEFAULT_RETRY_COUNT,
    DEFAULT_RETRY_DELAY,
    DEFAULT_TIMEOUT,
    KEYCHAIN_ACCOUNT,
    KEYCHAIN_SERVICE,
)

logger = logging.getLogger("zdrovena.common")


class FakturowniaClient:
    """Synchronous client for the Fakturownia REST API."""

    def __init__(
        self,
        api_token: str,
        domain: str = DEFAULT_DOMAIN,
        *,
        retry_count: int = DEFAULT_RETRY_COUNT,
        retry_delay: float = DEFAULT_RETRY_DELAY,
        timeout: int = DEFAULT_TIMEOUT,
        per_page: int = DEFAULT_PER_PAGE,
        pdf_delay: float = DEFAULT_PDF_DELAY,
    ) -> None:
        self.api_token = api_token
        self.base_url = f"https://{domain}"
        self.retry_count = retry_count
        self.retry_delay = retry_delay
        self.timeout = timeout
        self.per_page = per_page
        self.pdf_delay = pdf_delay
        self.session = requests.Session()

    # ── Factory: create from macOS Keychain ──────────────────────────────────

    @classmethod
    def from_keyring(
        cls,
        *,
        domain: str = DEFAULT_DOMAIN,
        service: str = KEYCHAIN_SERVICE,
        account: str = KEYCHAIN_ACCOUNT,
        **kwargs: Any,
    ) -> FakturowniaClient:
        """
        Create a client using the API token stored in macOS Keychain.

        Reads the token via the ``keyring`` library, which maps to
        macOS Keychain, GNOME Keyring, or Windows Credential Locker.

        Raises
        ------
        RuntimeError
            If the token is not found in the Keychain.
        """
        token = keyring.get_password(service, account)
        if not token:
            raise RuntimeError(
                f"Fakturownia API token not found in Keychain "
                f"(service={service!r}, account={account!r}). "
                f"Store it with:  security add-generic-password "
                f"-s {service} -a {account} -w 'YOUR_TOKEN'"
            )
        return cls(api_token=token, domain=domain, **kwargs)

    # ── Low-level request with retry ─────────────────────────────────────────

    def _request(
        self,
        method: str,
        endpoint: str,
        params: dict | None = None,
        *,
        stream: bool = False,
    ) -> requests.Response:
        """Execute an HTTP request with exponential-backoff retry."""
        url = f"{self.base_url}/{endpoint}"
        if params is None:
            params = {}
        params["api_token"] = self.api_token

        delay = self.retry_delay
        last_exc: Exception | None = None

        for attempt in range(1, self.retry_count + 1):
            try:
                safe = {
                    k: ("***" if k == "api_token" else v)
                    for k, v in params.items()
                }
                logger.debug(
                    "→ %s %s params=%s (attempt %d)", method, url, safe, attempt
                )
                resp = self.session.request(
                    method,
                    url,
                    params=params,
                    timeout=self.timeout,
                    stream=stream,
                )
                resp.raise_for_status()
                return resp
            except requests.exceptions.RequestException as exc:
                last_exc = exc
                logger.warning(
                    "Request failed (attempt %d/%d): %s",
                    attempt,
                    self.retry_count,
                    exc,
                )
                if attempt < self.retry_count:
                    time.sleep(delay)
                    delay *= 2

        raise RuntimeError(
            f"Fakturownia API request failed after {self.retry_count} "
            f"attempts: {last_exc}"
        )

    def get_json(
        self, endpoint: str, params: dict | None = None
    ) -> Any:
        """GET request → parsed JSON response."""
        resp = self._request("GET", endpoint, params=params)
        return resp.json()

    def get_binary(
        self, endpoint: str, params: dict | None = None
    ) -> bytes:
        """GET request → raw binary content (for XLS downloads etc.)."""
        resp = self._request("GET", endpoint, params=params)
        return resp.content

    # ── Paginated invoice fetch ──────────────────────────────────────────────

    def fetch_invoices(
        self,
        date_from: str,
        date_to: str,
        *,
        income: str = "yes",
        label: str = "invoices",
        extra_params: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Fetch all invoices in a date range (paginated).

        Parameters
        ----------
        date_from, date_to : "YYYY-MM-DD"
        income    : "yes" for sales, "no" for costs/expenses
        label     : logging label
        extra_params : additional query params merged into every page request
        """
        all_invoices: list[dict] = []
        page = 1

        while True:
            params: dict[str, Any] = {
                "period": "more",
                "date_from": date_from,
                "date_to": date_to,
                "page": str(page),
                "per_page": str(self.per_page),
                "income": income,
                "include_positions": "true",
            }
            if extra_params:
                params.update(extra_params)

            data = self.get_json("invoices.json", params)

            if not isinstance(data, list) or not data:
                break

            all_invoices.extend(data)
            logger.info("Page %d: fetched %d %s", page, len(data), label)

            if len(data) < self.per_page:
                break
            page += 1

        logger.info("Total %s: %d", label, len(all_invoices))
        return all_invoices

    # ── Convenience wrappers ─────────────────────────────────────────────────

    def fetch_sales_invoices(
        self, date_from: str, date_to: str
    ) -> list[dict[str, Any]]:
        """Fetch all *sales* invoices for a date range."""
        return self.fetch_invoices(
            date_from, date_to, income="yes", label="sales invoices"
        )

    def fetch_cost_invoices(
        self, date_from: str, date_to: str
    ) -> list[dict[str, Any]]:
        """Fetch all *cost/expense* invoices for a date range."""
        return self.fetch_invoices(
            date_from,
            date_to,
            income="no",
            label="cost invoices",
            extra_params={
                "additional_fields[invoice]": "gov_id,gov_status",
            },
        )

    # ── PDF downloads ────────────────────────────────────────────────────────

    def download_pdf(self, invoice_id: int, save_path: Path) -> Path:
        """Download a single invoice PDF."""
        resp = self._request("GET", f"invoices/{invoice_id}.pdf", stream=True)
        save_path.parent.mkdir(parents=True, exist_ok=True)
        with open(save_path, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=8192):
                fh.write(chunk)
        logger.debug("Saved PDF → %s", save_path)
        return save_path

    def download_all_pdfs(
        self,
        invoices: list[dict[str, Any]],
        target_dir: Path,
        *,
        dry_run: bool = False,
    ) -> list[Path]:
        """Download PDFs for a list of invoices into *target_dir*."""
        target_dir.mkdir(parents=True, exist_ok=True)
        saved: list[Path] = []
        seen: set[str] = set()

        for idx, inv in enumerate(invoices, 1):
            inv_id = inv["id"]
            number: str = inv.get("number", str(inv_id))
            if number in seen:
                logger.warning("Duplicate invoice number %s – skipped", number)
                continue
            seen.add(number)

            safe_name = (
                number.replace("/", "_").replace("\\", "_").replace(" ", "_")
            )
            pdf_path = target_dir / f"{safe_name}.pdf"

            if dry_run:
                logger.info("[DRY-RUN] Would download: %s", pdf_path.name)
                continue

            if pdf_path.exists():
                logger.debug("Already exists, skipping: %s", pdf_path.name)
                saved.append(pdf_path)
                continue

            self.download_pdf(inv_id, pdf_path)
            saved.append(pdf_path)
            logger.info(
                "[%d/%d] Downloaded: %s", idx, len(invoices), pdf_path.name
            )
            time.sleep(self.pdf_delay)

        return saved
