"""zdrovena.common.fakturownia — Fakturownia REST API v1 client.

Docs:
    https://app.fakturownia.pl/api
    https://github.com/fakturownia/API
    https://pomoc.fakturownia.pl/pola-przekazywane-z-programu-fakturownia-do-ksef-zgodnie-ze-schema-fa-3

Auth model:
    Simple API token — passed via `api_token` query param OR JSON body key.
    We use query param for GET, and both query + body wrapper for PUT/POST.

Key concept: `settlement_positions` field
    KSeF FA(3) node "Rozliczenie" — list of `{kind, amount, description}` where
    kind ∈ {"charge" (obciążenie), "deduction" (odliczenie)}.
    Confirmed by official Fakturownia field mapping (2026-06-11).
    We use this field to add "Kaucja za opakowania zwrotne" to Allegro invoices
    where the deposit is not automatically calculated by Fakturownia.

Environment vars:
    FAKTUROWNIA_BASE_URL   e.g. https://zdrovena.fakturownia.pl
    FAKTUROWNIA_API_TOKEN  personal API token
    FAKTUROWNIA_HTTP_TIMEOUT   (default 30 seconds)
"""

from __future__ import annotations

import logging
import os
from decimal import Decimal
from http import HTTPStatus
from typing import Any

import requests

from zdrovena.common.shipping_exceptions import (
    CourierConnectionError,
    CourierTimeoutError,
    FakturowniaAuthError,
    FakturowniaBusinessError,
    FakturowniaServerError,
)

log = logging.getLogger(__name__)


# ── KSeF Rozliczenie constants ────────────────────────────────────────────────

SETTLEMENT_KIND_CHARGE = "charge"  # KSeF Obciazenia
SETTLEMENT_KIND_DEDUCTION = "deduction"  # KSeF Odliczenia
_VALID_SETTLEMENT_KINDS = {SETTLEMENT_KIND_CHARGE, SETTLEMENT_KIND_DEDUCTION}


class FakturowniaClient:
    """Thin REST client for Fakturownia.

    All requests go through a single `requests.Session` so tests can patch
    `requests.Session.request` in one place.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_token: str,
        timeout: int | float = 30,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token
        self.timeout = timeout
        self._session = session or requests.Session()

    # ── construction from env ────────────────────────────────────────────────

    @classmethod
    def from_env(cls) -> FakturowniaClient:
        base = os.getenv("FAKTUROWNIA_BASE_URL", "").strip()
        token = os.getenv("FAKTUROWNIA_API_TOKEN", "").strip()
        if not token:
            raise RuntimeError(
                "FAKTUROWNIA_API_TOKEN env var missing — cannot init FakturowniaClient"
            )
        if not base:
            base = "https://zdrovena.fakturownia.pl"
        timeout = int(os.getenv("FAKTUROWNIA_HTTP_TIMEOUT", "30"))
        return cls(base_url=base, api_token=token, timeout=timeout)

    # ── low-level request helpers ────────────────────────────────────────────

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        raw: bool = False,
    ) -> Any:
        url = f"{self.base_url}{path}"
        # api_token in query for GET; both places acceptable per Fakturownia docs.
        merged_params = {"api_token": self.api_token, **(params or {})}
        try:
            resp = self._session.request(
                method=method,
                url=url,
                params=merged_params,
                json=json,
                timeout=self.timeout,
            )
        except requests.Timeout as e:
            raise CourierTimeoutError(courier="fakturownia", action=method.lower()) from e
        except requests.ConnectionError as e:
            raise CourierConnectionError(courier="fakturownia", detail=str(e)) from e

        return self._parse_response(resp, method=method, path=path, raw=raw)

    @staticmethod
    def _parse_response(
        resp: requests.Response, *, method: str, path: str, raw: bool = False
    ) -> Any:
        status = resp.status_code
        if HTTPStatus.OK <= status < HTTPStatus.MULTIPLE_CHOICES:
            if raw:
                return resp.content
            if status == HTTPStatus.NO_CONTENT:
                return None
            try:
                return resp.json()
            except ValueError:
                return None
        # error mapping
        try:
            body = resp.json()
        except ValueError:
            body = {"text": (resp.text or "")[:200]}
        detail = f"{method} {path} → {status}: {body}"
        if status in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
            raise FakturowniaAuthError(detail=detail)
        if status >= HTTPStatus.INTERNAL_SERVER_ERROR:
            raise FakturowniaServerError(status=status)
        # everything else (400, 404, 422, ...) is business error
        raise FakturowniaBusinessError(detail=detail, action=f"{method.lower()} {path}")

    # ── high-level API ───────────────────────────────────────────────────────

    def get_invoice(self, invoice_id: int) -> dict[str, Any]:
        return self._request("GET", f"/invoices/{invoice_id}.json")

    def get_invoice_pdf(self, invoice_id: int) -> bytes:
        """Download the invoice PDF. Returns raw PDF bytes."""
        return self._request("GET", f"/invoices/{invoice_id}.pdf", raw=True)

    def list_invoices(
        self,
        *,
        period: str | None = None,
        page: int | None = None,
        per_page: int | None = None,
        number: str | None = None,
        oid: str | None = None,
        include_positions: bool | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if period is not None:
            params["period"] = period
        if page is not None:
            params["page"] = page
        if per_page is not None:
            params["per_page"] = per_page
        if number is not None:
            params["number"] = number
        if oid is not None:
            params["oid"] = oid
        if include_positions is not None:
            params["include_positions"] = "true" if include_positions else "false"
        return self._request("GET", "/invoices.json", params=params)

    def update_invoice(self, invoice_id: int, patch: dict[str, Any]) -> dict[str, Any]:
        body = {"api_token": self.api_token, "invoice": patch}
        return self._request("PUT", f"/invoices/{invoice_id}.json", json=body)

    def create_invoice(self, invoice: dict[str, Any]) -> dict[str, Any]:
        """Create a new Fakturownia document (VAT invoice, nota księgowa, etc.).

        `invoice["kind"]` selects the document type (e.g. "vat", "accounting_note").
        Returns the created document, including its `id`.
        """
        body = {"api_token": self.api_token, "invoice": invoice}
        return self._request("POST", "/invoices.json", json=body)

    # ── settlement_positions (KSeF Rozliczenie) ─────────────────────────────

    def add_settlement_position(
        self,
        *,
        invoice_id: int,
        kind: str,
        amount_pln: str | float | Decimal,
        description: str,
    ) -> dict[str, Any]:
        """Append a single settlement row (obciążenie / odliczenie) to invoice.

        1. GET current invoice to read existing `settlement_positions`
        2. Build new list = existing (preserving `id` on each) + new row
        3. PUT invoice with the merged list

        Raises:
            ValueError: on invalid kind / amount / description
            FakturowniaAuthError / FakturowniaBusinessError / FakturowniaServerError
        """
        if kind not in _VALID_SETTLEMENT_KINDS:
            raise ValueError(f"kind must be one of {_VALID_SETTLEMENT_KINDS}, got {kind!r}")

        amount_str = _normalize_amount_pln(amount_pln)
        if Decimal(amount_str) <= 0:
            raise ValueError(f"amount_pln must be > 0, got {amount_pln!r}")

        if not description or not description.strip():
            raise ValueError("description must be non-empty")

        current = self.get_invoice(invoice_id)
        existing_rows = current.get("settlement_positions") or []

        # Race protection: idempotency re-check. Wołający (patcher) sprawdza
        # `has_settlement_with_description` przed patchem, ale między tym check-em a
        # naszym PUT-em inny worker/retry mógł dodać tę samą pozycję. Robimy drugi
        # check tu, na świeżo pobranej fakturze — jeśli pozycja z tym opisem już jest,
        # zwracamy fakturę bez PUT-a (idempotent no-op).
        needle = description.strip().casefold()
        for row in existing_rows:
            desc = (row.get("description") or "").strip().casefold()
            if desc == needle:
                log.info(
                    "add_settlement_position: invoice %s already has row %r — skipping PUT",
                    invoice_id,
                    description.strip(),
                )
                return current

        # Preserve existing rows verbatim (Rails PUT semantics require `id` on kept rows).
        merged: list[dict[str, Any]] = list(existing_rows)
        merged.append(
            {
                "kind": kind,
                "amount": amount_str,
                "description": description.strip(),
            }
        )

        return self.update_invoice(invoice_id, {"settlement_positions": merged})

    @staticmethod
    def has_settlement_with_description(invoice: dict[str, Any], description: str) -> bool:
        """Return True iff invoice.settlement_positions contains a row whose
        description matches (case-insensitive, stripped)."""
        needle = (description or "").strip().casefold()
        if not needle:
            return False
        rows = invoice.get("settlement_positions") or []
        for row in rows:
            desc = (row.get("description") or "").strip().casefold()
            if desc == needle:
                return True
        return False


# ── helpers ───────────────────────────────────────────────────────────────────


def _normalize_amount_pln(amount: str | float | Decimal) -> str:
    """Coerce PLN amount to string with exactly 2 decimal places (banker-safe)."""
    if isinstance(amount, Decimal):
        d = amount
    elif isinstance(amount, str):
        d = Decimal(amount.strip())
    else:
        # float — go through str() to avoid binary noise like 5.5 → 5.4999...
        d = Decimal(str(amount))
    return f"{d:.2f}"
