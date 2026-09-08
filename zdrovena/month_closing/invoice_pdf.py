"""Tell a cost invoice apart from the mail that arrives beside it.

A vendor mailbox search for "inpost" or a supplier address matches damage
protocols, claim replies and marketing reports in the same threads as the
invoice, and every PDF in those threads used to land in the month's cost folder
and go to the accountant.

The classifier is deliberately asymmetric. Rejecting a real invoice costs a
missing cost document in the books; keeping a stray PDF costs the accountant one
glance. So only the confident case is rejected: text we could read that plainly
carries no invoice shape. Anything unreadable — a scan with no text layer, a
file that is not a PDF at all — is kept and reported.

Recognition cannot key on the word "faktura". Two of the three real vendors
never write it: InPost renders its invoices with a broken text encoding, and
Shopify bills in English. What every invoice does carry is a tax id and a set of
amounts, so those are the signals.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass

logger = logging.getLogger("zdrovena.month_closing.invoice_pdf")

# Amounts, comma-decimal (Polish) and dot-decimal (Shopify). The lookarounds
# keep "31.07.2026" out: a date must never read as two amounts.
_AMOUNT_COMMA = re.compile(r"(?<![\d.,])\d{1,3}(?:[  .]\d{3})*,\d{2}(?![\d,])")
_AMOUNT_DOT = re.compile(r"(?<![\d.,])\d{1,3}(?:[  ,]\d{3})*\.\d{2}(?![\d.])")

# A tax id is exactly ten digits. The boundary is the whole point: a parcel
# number is 24 digits and a phone is 11, and either would otherwise look like a
# NIP sitting inside a damage protocol.
_TAX_ID_PLAIN = re.compile(r"(?<!\d)\d{10}(?!\d)")
_TAX_ID_DASHED = re.compile(r"(?<!\d)(?:\d{3}-\d{3}-\d{2}-\d{2}|\d{3}-\d{2}-\d{2}-\d{3})(?!\d)")

# Present in some invoices, absent from two of the three real vendors — a bonus
# signal, never a requirement.
_INVOICE_WORDS = re.compile(r"faktur|invoice|rachunek|nota\s+ksieg|nota\s+księg", re.IGNORECASE)

# Below two amounts there is no netto/VAT/brutto shape to speak of. A damage
# protocol carries exactly one ("wartość deklarowana"), which is why the floor
# is two rather than one.
_MIN_AMOUNTS = 2

# Shorter than this, an extracted text is not a document we managed to read.
_MIN_TEXT_LENGTH = 20


@dataclass(frozen=True)
class InvoiceVerdict:
    """Whether a PDF may be filed as a cost invoice, and why."""

    is_invoice: bool
    reason: str


def _count_amounts(text: str) -> int:
    return len(_AMOUNT_COMMA.findall(text)) + len(_AMOUNT_DOT.findall(text))


def _has_tax_id(text: str) -> bool:
    return bool(_TAX_ID_PLAIN.search(text) or _TAX_ID_DASHED.search(text))


def classify_invoice_text(text: str) -> InvoiceVerdict:
    """Judge extracted text. Both signals must be present to file it."""

    amounts = _count_amounts(text)
    if amounts < _MIN_AMOUNTS:
        return InvoiceVerdict(
            False,
            f"brak kwot faktury (znaleziono {amounts}, potrzeba {_MIN_AMOUNTS})",
        )
    if not _has_tax_id(text):
        return InvoiceVerdict(False, "brak numeru NIP (dziesięciu cyfr) w treści")

    if _INVOICE_WORDS.search(text):
        return InvoiceVerdict(True, "kwoty, NIP i słowo „faktura” w treści")
    return InvoiceVerdict(True, f"kwoty ({amounts}) i NIP w treści")


def classify_invoice_pdf(content: bytes) -> InvoiceVerdict:
    """Judge raw PDF bytes. Anything unreadable is kept, never discarded."""

    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content))
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception as exc:
        logger.info("Invoice classification could not read the PDF: %s", exc)
        return InvoiceVerdict(True, "nie udało się odczytać pliku jako PDF — zachowany")

    if len(text.strip()) < _MIN_TEXT_LENGTH:
        return InvoiceVerdict(True, "PDF bez warstwy tekstowej (skan?) — zachowany")

    return classify_invoice_text(text)
