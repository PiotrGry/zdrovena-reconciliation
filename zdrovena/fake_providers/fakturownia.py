"""Existing Fakturownia fake, kept separate from shipping provider emulators."""

from __future__ import annotations

import io
import zipfile
from copy import deepcopy
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

from zdrovena.fake_providers.common import PDF_BYTES, apply_http_scenario
from zdrovena.fake_providers.state import STATE

router = APIRouter(prefix="/fakturownia")


def _require_token(api_token: str | None) -> None:
    if not api_token:
        raise HTTPException(status_code=401, detail="api_token required")


@router.get("/invoices.json")
def list_invoices(
    api_token: str | None = None,
    number: str | None = None,
    oid: str | None = None,
    income: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    page: int = 1,
    per_page: int = 100,
) -> list[dict[str, Any]]:
    _require_token(api_token)
    invoices = list(STATE.fakturownia_invoices.values())
    if number:
        invoices = [invoice for invoice in invoices if invoice.get("number") == number]
    if oid:
        invoices = [invoice for invoice in invoices if invoice.get("oid") == oid]
    if income:
        invoices = [invoice for invoice in invoices if invoice.get("income", "yes") == income]
    if date_from:
        invoices = [
            invoice
            for invoice in invoices
            if (invoice.get("sell_date") or invoice.get("issue_date") or "") >= date_from
        ]
    if date_to:
        invoices = [
            invoice
            for invoice in invoices
            if (invoice.get("sell_date") or invoice.get("issue_date") or "") <= date_to
        ]
    start = max(page - 1, 0) * per_page
    return deepcopy(invoices[start : start + per_page])


@router.post("/invoices.json")
async def create_invoice(request: Request, api_token: str | None = None) -> dict[str, Any]:
    _require_token(api_token)
    scenario = STATE.scenario("fakturownia", "create_invoice")
    apply_http_scenario("fakturownia", "create_invoice", scenario)
    body = await request.json()
    invoice = body.get("invoice")
    if not isinstance(invoice, dict):
        raise HTTPException(status_code=422, detail="invoice object required")
    oid = invoice.get("oid")
    if oid:
        existing = next(
            (item for item in STATE.fakturownia_invoices.values() if item.get("oid") == oid),
            None,
        )
        if existing:
            if scenario == "already_exists":
                return deepcopy(existing)
            raise HTTPException(status_code=422, detail="invoice already exists")
    internal_id = STATE.next_id("fakturownia-invoice")
    created = {
        "id": int(internal_id.rsplit("-", 1)[-1]),
        "number": invoice.get("number") or internal_id,
        **invoice,
    }
    STATE.fakturownia_invoices[str(created["id"])] = created
    return deepcopy(created)


@router.get("/invoices/{invoice_id}.json")
def get_invoice(invoice_id: int, api_token: str | None = None) -> dict[str, Any]:
    _require_token(api_token)
    invoice = STATE.fakturownia_invoices.get(str(invoice_id))
    if not invoice:
        raise HTTPException(status_code=404, detail="invoice not found")
    return deepcopy(invoice)


@router.put("/invoices/{invoice_id}.json")
async def update_invoice(
    invoice_id: int,
    request: Request,
    api_token: str | None = None,
) -> dict[str, Any]:
    _require_token(api_token)
    body = await request.json()
    patch = body.get("invoice")
    if not isinstance(patch, dict):
        raise HTTPException(status_code=422, detail="invoice object required")
    invoice = STATE.fakturownia_invoices.setdefault(str(invoice_id), {"id": invoice_id})
    invoice.update(patch)
    return deepcopy(invoice)


@router.post("/invoices/{invoice_id}/change_status.json")
def change_status(
    invoice_id: int,
    status: str,
    api_token: str | None = None,
) -> dict[str, Any]:
    _require_token(api_token)
    invoice = STATE.fakturownia_invoices.get(str(invoice_id))
    if not invoice:
        raise HTTPException(status_code=404, detail="invoice not found")
    invoice["status"] = status
    return deepcopy(invoice)


@router.get("/invoices/{invoice_id}.pdf")
def invoice_pdf(invoice_id: int, api_token: str | None = None) -> Response:
    _require_token(api_token)
    if str(invoice_id) not in STATE.fakturownia_invoices:
        raise HTTPException(status_code=404, detail="invoice not found")
    return Response(PDF_BYTES, media_type="application/pdf")


@router.get("/invoices/{invoice_id}/attachments_zip.json")
def attachments(invoice_id: int, api_token: str | None = None) -> Response:
    _require_token(api_token)
    invoice = STATE.fakturownia_invoices.get(str(invoice_id))
    if not invoice:
        raise HTTPException(status_code=404, detail="invoice not found")
    if not invoice.get("has_attachments"):
        raise HTTPException(status_code=404, detail="invoice has no attachments")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        number = str(invoice.get("number") or invoice_id).replace("/", "_")
        archive.writestr(f"original-{number}.pdf", PDF_BYTES)
    return Response(buffer.getvalue(), media_type="application/zip")
