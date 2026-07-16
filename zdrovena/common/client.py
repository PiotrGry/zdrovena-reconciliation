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

import io
import logging
import os
import re
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from zdrovena.common.config import (
    DEFAULT_DOMAIN,
    DEFAULT_PDF_DELAY,
    DEFAULT_PER_PAGE,
    DEFAULT_RETRY_COUNT,
    DEFAULT_RETRY_DELAY,
    DEFAULT_TIMEOUT,
    KEYCHAIN_SERVICE,
)
from zdrovena.common.exceptions import ApiResponseFormatError
from zdrovena.common.retry import retry_request
from zdrovena.common.secrets import get_secret

logger = logging.getLogger("zdrovena.common")


@dataclass(frozen=True, slots=True)
class DownloadedCostDocument:
    """One selected cost document and the source used for the final package."""

    path: Path
    invoice_id: int
    invoice_number: str
    vendor: str
    source_kind: str


class FakturowniaClient:
    """Synchronous client for the Fakturownia REST API."""

    def __init__(
        self,
        api_token: str,
        domain: str | None = None,
        *,
        base_url: str | None = None,
        retry_count: int = DEFAULT_RETRY_COUNT,
        retry_delay: float = DEFAULT_RETRY_DELAY,
        timeout: int = DEFAULT_TIMEOUT,
        per_page: int = DEFAULT_PER_PAGE,
        pdf_delay: float = DEFAULT_PDF_DELAY,
    ) -> None:
        self.api_token = api_token
        if base_url:
            resolved_base_url = base_url
        elif domain:
            resolved_base_url = f"https://{domain}"
        else:
            resolved_base_url = (
                os.environ.get("FAKTUROWNIA_BASE_URL", "").strip() or f"https://{DEFAULT_DOMAIN}"
            )
        self.base_url = resolved_base_url.rstrip("/")
        self.retry_count = retry_count
        self.retry_delay = retry_delay
        self.timeout = timeout
        self.per_page = per_page
        self.pdf_delay = pdf_delay
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "zdrovena-reconciliation/2.0"

    # ── Factory: create from macOS Keychain ──────────────────────────────────

    @classmethod
    def from_keyring(
        cls,
        *,
        domain: str | None = None,
        base_url: str | None = None,
        service: str = KEYCHAIN_SERVICE,
        **kwargs: Any,
    ) -> FakturowniaClient:
        """Create a client using the API token from env, Keychain, or Key Vault.

        Delegates to get_secret() which resolves: env var → keyring → Azure Key Vault.

        Raises
        ------
        MissingSecretError
            If the token is not found in any location.
        """
        token = get_secret(service)
        return cls(api_token=token, domain=domain, base_url=base_url, **kwargs)

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

        safe = {k: ("***" if k == "api_token" else v) for k, v in params.items()}
        logger.debug("→ %s %s params=%s", method, url, safe)

        return retry_request(
            self.session,
            method,
            url,
            max_retries=self.retry_count,
            initial_delay=self.retry_delay,
            timeout=self.timeout,
            caller="Fakturownia",
            params=params,
            stream=stream,
        )

    def get_json(self, endpoint: str, params: dict | None = None) -> Any:
        """GET request → parsed JSON response.

        Raises
        ------
        ApiResponseFormatError
            If the response body is not valid JSON.
        """
        resp = self._request("GET", endpoint, params=params)
        try:
            return resp.json()
        except ValueError:
            # Sanitize body: strip potential api_token leak
            raw = resp.text[:200]
            raw = re.sub(r"api_token=[^&\s]+", "api_token=***", raw)
            raise ApiResponseFormatError(resp.status_code, raw) from None

    def get_binary(self, endpoint: str, params: dict | None = None) -> bytes:
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

    def fetch_sales_invoices(self, date_from: str, date_to: str) -> list[dict[str, Any]]:
        """Fetch all *sales* invoices for a date range."""
        return self.fetch_invoices(date_from, date_to, income="yes", label="sales invoices")

    def fetch_cost_invoices(self, date_from: str, date_to: str) -> list[dict[str, Any]]:
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

    def download_original_attachments(
        self,
        invoice_id: int,
        target_dir: Path,
        *,
        filename_prefix: str,
    ) -> list[Path]:
        """Download original PDF attachments assigned to a Fakturownia expense.

        Fakturownia returns all attachments as a ZIP archive. Only regular PDF
        files are extracted and every archive path is reduced to its basename,
        so a malformed archive cannot write outside ``target_dir``.
        """
        resp = self._request(
            "GET",
            f"invoices/{invoice_id}/attachments_zip.json",
            stream=True,
        )
        target_dir.mkdir(parents=True, exist_ok=True)
        try:
            archive = zipfile.ZipFile(io.BytesIO(resp.content))
        except zipfile.BadZipFile as exc:
            raise RuntimeError(
                f"Fakturownia returned an invalid attachment archive for invoice {invoice_id}"
            ) from exc

        saved: list[Path] = []
        with archive:
            members = [
                member
                for member in archive.infolist()
                if not member.is_dir() and Path(member.filename).suffix.casefold() == ".pdf"
            ]
            if not members:
                raise RuntimeError(
                    f"No original PDF attachment found for Fakturownia invoice {invoice_id}"
                )
            for idx, member in enumerate(members, 1):
                original_name = Path(member.filename).name
                safe_original = re.sub(r"[^A-Za-z0-9._-]+", "_", original_name).strip("._")
                if not safe_original:
                    safe_original = f"attachment-{idx}.pdf"
                dest = target_dir / f"{filename_prefix}__original__{safe_original}"
                payload = archive.read(member)
                if dest.exists() and dest.read_bytes() == payload:
                    saved.append(dest)
                    continue
                dest.write_bytes(payload)
                saved.append(dest)
                logger.info(
                    "Downloaded original attachment for invoice %s → %s",
                    invoice_id,
                    dest.name,
                )
        return saved

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

            safe_name = number.replace("/", "_").replace("\\", "_").replace(" ", "_")
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
            logger.info("[%d/%d] Downloaded: %s", idx, len(invoices), pdf_path.name)
            time.sleep(self.pdf_delay)

        return saved

    def download_cost_pdfs(
        self,
        invoices: list[dict[str, Any]],
        target_dir: Path,
        *,
        dry_run: bool = False,
    ) -> list[Path]:
        """Compatibility wrapper returning selected cost-document paths."""
        return [
            item.path
            for item in self.download_cost_documents(
                invoices,
                target_dir,
                dry_run=dry_run,
            )
        ]

    def download_cost_documents(
        self,
        invoices: list[dict[str, Any]],
        target_dir: Path,
        *,
        dry_run: bool = False,
        source_policy: Callable[[dict[str, Any]], str] | None = None,
    ) -> list[DownloadedCostDocument]:
        """Select and download one source family for every cost invoice.

        ``source_policy`` returns ``original_preferred``, ``original_required``
        or ``generated``.
        """

        target_dir.mkdir(parents=True, exist_ok=True)
        saved: list[DownloadedCostDocument] = []
        seen: set[str] = set()

        for idx, inv in enumerate(invoices, 1):
            inv_id = int(inv["id"])
            number: str = inv.get("number", str(inv_id))
            if number in seen:
                logger.warning("Duplicate invoice number %s – skipped", number)
                continue
            seen.add(number)

            safe_name = self.cost_document_stem(inv)
            pdf_path = target_dir / f"{safe_name}.pdf"
            vendor = str(inv.get("buyer_name") or "unknown")
            policy = source_policy(inv) if source_policy else "original_preferred"
            has_attachments = bool(inv.get("has_attachments"))

            if dry_run:
                source_kind = (
                    "original_attachment"
                    if has_attachments and policy != "generated"
                    else "generated_pdf"
                )
                logger.info("[DRY-RUN] Would download %s from %s", number, source_kind)
                continue

            if has_attachments and policy != "generated":
                try:
                    original_paths = self.download_original_attachments(
                        inv_id,
                        target_dir,
                        filename_prefix=safe_name,
                    )
                except Exception:
                    if policy == "original_required":
                        raise
                    logger.warning(
                        "Could not download original attachment for %s; "
                        "falling back to generated PDF",
                        number,
                        exc_info=True,
                    )
                else:
                    saved.extend(
                        DownloadedCostDocument(
                            path=path,
                            invoice_id=inv_id,
                            invoice_number=number,
                            vendor=vendor,
                            source_kind="original_attachment",
                        )
                        for path in original_paths
                    )
                    logger.info(
                        "[%d/%d] Downloaded %d original attachment(s): %s",
                        idx,
                        len(invoices),
                        len(original_paths),
                        number,
                    )
                    time.sleep(self.pdf_delay)
                    continue

            if policy == "original_required":
                logger.warning(
                    "Original attachment required but unavailable: %s (%s)",
                    number,
                    vendor,
                )
                continue

            if not pdf_path.exists():
                self.download_pdf(inv_id, pdf_path)
            else:
                logger.debug("Already exists, skipping: %s", pdf_path.name)
            saved.append(
                DownloadedCostDocument(
                    path=pdf_path,
                    invoice_id=inv_id,
                    invoice_number=number,
                    vendor=vendor,
                    source_kind="generated_pdf",
                )
            )
            logger.info("[%d/%d] Downloaded generated PDF: %s", idx, len(invoices), pdf_path.name)
            time.sleep(self.pdf_delay)

        return saved

    @staticmethod
    def cost_document_stem(inv: dict[str, Any]) -> str:
        """Return the stable vendor-and-number stem used for cost documents."""
        vendor = str(inv.get("buyer_name") or "unknown")[:30]
        safe_vendor = vendor.replace(" ", "_").replace("/", "_").replace(".", "").replace(",", "")
        safe_number = (
            str(inv.get("number", inv["id"])).replace("/", "_").replace("\\", "_").replace(" ", "_")
        )
        return f"{safe_vendor}_{safe_number}"
