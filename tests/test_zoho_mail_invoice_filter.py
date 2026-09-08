"""The mail download files invoices, not everything that came in the thread.

A vendor search matches damage protocols and claim replies in the same threads
as the invoice. These tests drive the real classifier over real (minimal) PDFs;
only the network read is stubbed, because that is the one thing a test cannot
have.
"""

from __future__ import annotations

import io
import json
import logging

import pytest

from zdrovena.month_closing.zoho_mail import ZohoMailClient

INVOICE_LINES = [
    "FAKTURA VAT Nr 12345/8/AP/2026",
    "NIP 1111111111",
    "Netto 100,00 VAT 23,00 Brutto 123,00",
]
PROTOCOL_LINES = [
    "PROTOKOL SZKODY",
    "Dla przesylki nr: 630015287666400032517684",
    "Wartosc deklarowana: 100,00",
]


def make_pdf(lines: list[str]) -> bytes:
    """A one-page PDF with a real text layer, built without a PDF library."""
    content = (
        "BT /F1 12 Tf 10 180 Td " + " ".join(f"({line}) Tj 0 -14 Td" for line in lines) + " ET"
    )
    objects = [
        "<</Type/Catalog/Pages 2 0 R>>",
        "<</Type/Pages/Kids[3 0 R]/Count 1>>",
        "<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 220]/Contents 4 0 R"
        "/Resources<</Font<</F1 5 0 R>>>>>>",
        f"<</Length {len(content)}>>\nstream\n{content}\nendstream",
        "<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n{body}\nendobj\n".encode("latin-1"))
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<</Size {len(objects) + 1}/Root 1 0 R>>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return out.getvalue()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("INVOICE_PDF_FILTER", raising=False)
    mail = ZohoMailClient("id", "secret", "refresh")
    mail.access_token = "token"
    mail.account_id = "1"
    return mail


def _attachments(*names: str) -> list[dict]:
    return [
        {"attachmentName": name, "attachmentId": f"att-{index}"} for index, name in enumerate(names)
    ]


def _serve(client, payloads: dict[str, bytes]) -> None:
    """Return a body per attachment id, the only network read in this flow."""
    client._api_get_binary = lambda endpoint: payloads[endpoint.rsplit("/", 1)[-1]]


def _events(caplog) -> list[dict]:
    return [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == "zdrovena.events"
    ]


def test_a_damage_protocol_does_not_reach_the_cost_folder(client, tmp_path):
    _serve(client, {"att-0": make_pdf(INVOICE_LINES), "att-1": make_pdf(PROTOCOL_LINES)})

    saved, _ = client._save_pdf_attachments(
        _attachments("faktura.pdf", "protokol.pdf"), "/base", tmp_path, {}, "inpost"
    )

    assert [path.name for path in saved] == ["faktura.pdf"]
    assert not (tmp_path / "protokol.pdf").exists()


def test_the_rejection_names_the_file_and_the_reason(client, tmp_path, caplog):
    _serve(client, {"att-0": make_pdf(PROTOCOL_LINES)})

    with caplog.at_level(logging.INFO, logger="zdrovena.events"):
        client._save_pdf_attachments(_attachments("protokol.pdf"), "/base", tmp_path, {}, "inpost")

    rejection = next(
        event for event in _events(caplog) if event["event"] == "mail.attachment_rejected"
    )
    assert rejection["filename"] == "protokol.pdf"
    assert rejection["vendor"] == "inpost"
    assert rejection["reason"]


def test_an_attachment_we_cannot_read_is_still_filed(client, tmp_path):
    """A scan or a broken file is not a document we may throw away."""
    _serve(client, {"att-0": b"not a pdf at all"})

    saved, _ = client._save_pdf_attachments(
        _attachments("skan.pdf"), "/base", tmp_path, {}, "inpost"
    )

    assert [path.name for path in saved] == ["skan.pdf"]


def test_the_filter_can_be_turned_off_without_a_deploy(client, tmp_path, monkeypatch):
    monkeypatch.setenv("INVOICE_PDF_FILTER", "off")
    _serve(client, {"att-0": make_pdf(PROTOCOL_LINES)})

    saved, _ = client._save_pdf_attachments(
        _attachments("protokol.pdf"), "/base", tmp_path, {}, "inpost"
    )

    assert [path.name for path in saved] == ["protokol.pdf"]
