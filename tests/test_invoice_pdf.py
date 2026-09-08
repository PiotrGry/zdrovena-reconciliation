"""Tests for telling a cost invoice apart from the mail it arrives beside.

The samples below are shaped after the real July 2026 attachments — the vendor
mailbox carries damage protocols, claim replies and a marketing report in the
same threads a search for "inpost" or the supplier's address matches. Every
PDF in such a thread used to be saved into the month's cost folder and sent to
the accountant.

Names, addresses and identifiers here are invented. The shapes are what matter.
"""

from __future__ import annotations

import io

import pytest
from pypdf import PdfWriter

from zdrovena.month_closing.invoice_pdf import (
    classify_invoice_pdf,
    classify_invoice_text,
)

# ── Shapes that are invoices ─────────────────────────────────────────────────

APACZKA_STYLE = """
FAKTURA VAT Nr: 12345/8/AP/2026
Nr KSeF: 1111111111-20260817-3B31F2C000F1-5E
Wystawiona w dniu: 2026-08-17, Warszawa
Nabywca Przykladowa Firma, ul. Testowa 1, 00-001 Warszawa
NIP: 1111111111
Wartosc netto 100,00 VAT 23,00 Wartosc brutto 123,00
Razem do zaplaty 123,00 PLN
"""

# InPost renders its invoices with a broken text encoding, so the word
# "faktura" is not in the extracted text at all. Anything keyed to that word
# would throw away a real invoice.
INPOST_STYLE_MOJIBAKE = """
KRAKOW 2026-08-13 (7 dni) PEKAO 79 1240 6292 1111 0010 9304 7945
NIP 1111111111
LP Pozycja ,ORGu j.m. Cena netto bez rabatu Rabat :DUWRGuQHWWR Stawka Waluta
1 XVsXJLSDF]NRPDWRZH 100,00 23,00 123,00 PLN
Razem 100,00 23,00 123,00
"""

# Shopify bills in English and never writes "faktura" or "invoice" in the body.
SHOPIFY_STYLE_ENGLISH = """
TOTAL DUE EUR 179,43
Mastercard ending in 6035
* As recipient you are liable to account for reverse charge VAT
Tax ID 1111111111
OVERVIEW Add-ons (1 item) 17,00 Apps (4 items) 57,18
Subtotal 174,43 Tax 5,00 Total 179,43
"""

# ── Shapes that are not ──────────────────────────────────────────────────────

DAMAGE_PROTOCOL = """
PROTOKOL SZKODY
Dla przesylki nr: 630015287666400032517684
Oddzial: Gorzow Wielkopolski 1
Nadawca Imie i nazwisko: Jan Przykladowy Kraj: PL Miasto: Warszawa
Kod pocztowy: 00-001 Ulica: Testowa 1 Telefon: +48000000000
Wartosc deklarowana: 100,00
"""

CLAIM_REPLY = """
Dzien dobry! Warszawa, 31.07.2026 r.
Sprawdzilismy przesylke 620999679620819436455889 i ustalilismy, ze oplata za
nadanie nie jest naliczona, poniewaz paczka nie zostala nadana. W przypadku
klientow z umowa handlowa oplaty rozliczane sa zbiorczo.
Pozdrawiamy, Zespol Obslugi Klienta
"""

MARKETING_REPORT = """
Strategia sprzedazy i pozycjonowania
Raport przygotowany dla klienta
Rozdzial 1. Analiza rynku
Rozdzial 2. Rekomendacje
"""


class TestInvoicesAreKept:
    @pytest.mark.parametrize(
        ("label", "text"),
        [
            ("apaczka", APACZKA_STYLE),
            ("inpost with broken encoding", INPOST_STYLE_MOJIBAKE),
            ("shopify in english", SHOPIFY_STYLE_ENGLISH),
        ],
    )
    def test_a_real_invoice_is_recognised(self, label, text):
        assert classify_invoice_text(text).is_invoice, label

    def test_recognition_does_not_depend_on_the_word_invoice(self):
        """Two of three real vendors never write it. Keying on it loses them."""
        assert "faktur" not in INPOST_STYLE_MOJIBAKE.lower()
        assert "invoice" not in SHOPIFY_STYLE_ENGLISH.lower()
        assert classify_invoice_text(INPOST_STYLE_MOJIBAKE).is_invoice
        assert classify_invoice_text(SHOPIFY_STYLE_ENGLISH).is_invoice

    def test_a_one_line_invoice_still_passes(self):
        # The floor has to sit under the smallest real invoice: net, VAT, gross.
        minimal = "Faktura 1/2026\nNIP 1111111111\nNetto 10,00 VAT 2,30 Brutto 12,30"
        assert classify_invoice_text(minimal).is_invoice


class TestNonInvoicesAreRejected:
    @pytest.mark.parametrize(
        ("label", "text"),
        [
            ("damage protocol", DAMAGE_PROTOCOL),
            ("claim reply", CLAIM_REPLY),
            ("marketing report", MARKETING_REPORT),
        ],
    )
    def test_mail_that_travels_beside_an_invoice_is_rejected(self, label, text):
        verdict = classify_invoice_text(text)
        assert not verdict.is_invoice, label
        assert verdict.reason

    def test_a_parcel_number_is_not_read_as_a_tax_id(self):
        # 24 digits, not 10. The amounts are there on purpose: with them in
        # place the tax-id rule is the only thing left that can reject this
        # text, so a lost digit boundary shows up here instead of hiding
        # behind the amount floor.
        protocol_with_amounts = DAMAGE_PROTOCOL + "\nKoszt naprawy 100,00 20,00 120,00\n"

        verdict = classify_invoice_text(protocol_with_amounts)

        assert not verdict.is_invoice
        assert "nip" in verdict.reason.lower()

    def test_a_phone_number_is_not_read_as_a_tax_id(self):
        # 11 digits with the country code. One digit either side of a NIP is
        # the shape that a naive search finds first.
        assert not classify_invoice_text(
            "Telefon +48000000000\nKoszt 100,00 20,00 120,00"
        ).is_invoice

    def test_a_single_amount_is_not_an_invoice(self):
        # A damage protocol carries exactly one ("wartość deklarowana"). The
        # floor sits above it on purpose.
        verdict = classify_invoice_text("NIP 1111111111\nWartosc deklarowana: 100,00")

        assert not verdict.is_invoice
        assert "kwot" in verdict.reason.lower()

    def test_the_reason_names_what_was_missing(self):
        assert "kwot" in classify_invoice_text(MARKETING_REPORT).reason.lower()


class TestUnreadablePdfsAreKept:
    """A document we cannot read is not a document we may throw away.

    A scanned invoice has no text layer, and so does a photo. Only the
    confident case — text we can read that is plainly not an invoice — is
    rejected; anything else is kept and reported.
    """

    def test_a_pdf_with_no_text_layer_is_kept(self):
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=300)
        buffer = io.BytesIO()
        writer.write(buffer)

        verdict = classify_invoice_pdf(buffer.getvalue())

        assert verdict.is_invoice
        assert "tekst" in verdict.reason.lower()

    def test_bytes_that_are_not_a_pdf_are_kept(self):
        verdict = classify_invoice_pdf(b"not a pdf at all")

        assert verdict.is_invoice
        assert verdict.reason
