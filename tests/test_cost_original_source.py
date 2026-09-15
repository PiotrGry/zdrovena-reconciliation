"""Where a cost document came from, and why the operator can see it.

Since Poland moved to KSeF, Fakturownia attaches the original invoice as a
KSeF XML rather than a PDF. The collector asked for PDFs, found none, and fell
back to the generated PDF behind a log warning — so a nationwide change in the
law showed up as nothing at all for three months.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from zdrovena.common.client import FakturowniaClient
from zdrovena.month_closing.config import EXPECTED_VENDORS, match_vendor


def _archive(*names: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in names:
            archive.writestr(name, b"%PDF-1.4 or xml, does not matter here")
    return buffer.getvalue()


class TestVendorMatchingSurvivesPolishLetters:
    def test_the_accountants_own_invoice_matches_her_name(self):
        # config.py carried pattern="ogorzalek" while Fakturownia spells the
        # buyer "OGORZAŁEK". Plain substring matching never fired, so the
        # accountant's own invoice was treated as an unknown vendor.
        vendor = match_vendor("BIURO RACHUNKOWE MGR BOŻENA OGORZAŁEK", "")

        assert vendor is not None
        assert vendor.name == "Accounting/Bożena"

    def test_matching_still_works_without_diacritics(self):
        assert match_vendor("BIURO RACHUNKOWE OGORZALEK", "") is not None

    def test_an_unrelated_buyer_still_does_not_match(self):
        assert match_vendor("AXELL LOGISTICS SPÓŁKA Z O.O.", "") is None

    def test_a_vendor_is_found_by_tax_number_too(self):
        vendor = next(v for v in EXPECTED_VENDORS if v.name == "InPost")
        assert match_vendor("Nieznana nazwa", vendor.pattern) is not None


class TestPolkaDebitNotesReachTheCostFolder:
    """POLKA's kaucja debit notes exist only as email attachments.

    Two things kept every one of them out of the cost folder: no vendor told the
    Zoho phase to look at faktury@polskakaucja.pl, and the date gate deleted a
    "NOTA OBCIĄŻENIOWA" as "not an invoice" because it never says "faktura".
    """

    def test_the_notes_have_a_vendor_that_searches_the_mailbox(self):
        vendor = next(v for v in EXPECTED_VENDORS if v.name == "POLKA noty")

        assert vendor.email == "faktury@polskakaucja.pl"
        assert not (vendor.manual or vendor.skip or vendor.browser_download)

    def test_polkas_ksef_invoices_do_not_mark_the_notes_as_found(self):
        # Fakturownia lists the FSP invoices under this buyer. Matching them to the
        # notes vendor would skip the mailbox search in the Zoho phase.
        vendor = match_vendor(
            "POLKA Operator Systemu Kaucyjnego NOT FOR PROFIT Spolka Akcyjna ", "5252990128"
        )

        assert vendor is None or vendor.name != "POLKA noty"

    def test_the_date_gate_keeps_a_debit_note(self):
        from zdrovena.month_closing.invoice_date_check import is_likely_invoice

        text = (
            "NOTA OBCIĄŻENIOWA NR: NO/2026/09/77\nData wystawienia: 2026.09.15\n"
            "POLKA Operator Systemu Kaucyjnego NIP: 5252990128\n"
            "Kaucja - tworzywo sztuczne szt 474 0,50 237,00\nDo zapłaty: 237,00 PLN"
        )

        assert is_likely_invoice(Path("FV_NO_2026_09_77 Zdrovena.pdf"), text=text)

    def test_a_debit_note_from_anyone_else_is_still_dropped(self):
        # Owner's decision: a debit note is a cost only when POLKA issues it.
        from zdrovena.month_closing.invoice_date_check import is_likely_invoice

        text = (
            "NOTA OBCIĄŻENIOWA NR: 12/2026\nData wystawienia: 2026.09.15\n"
            "Inna Firma sp. z o.o. NIP: 1234567890\nDo zapłaty: 150,00 PLN"
        )

        assert not is_likely_invoice(Path("nota.pdf"), text=text)

    def test_the_date_gate_still_drops_a_document_that_is_no_cost(self):
        from zdrovena.month_closing.invoice_date_check import is_likely_invoice

        text = "Protokół szkody przesyłki 123456789012345678901234, wartość 150,00"

        assert not is_likely_invoice(Path("protokol.pdf"), text=text)


class TestAKsefXmlOriginalIsNotSilentlyIgnored:
    def _client(self, archive_bytes: bytes) -> FakturowniaClient:
        client = FakturowniaClient.__new__(FakturowniaClient)
        response = MagicMock()
        response.content = archive_bytes
        client._request = MagicMock(return_value=response)  # type: ignore[method-assign]
        return client

    def test_an_xml_only_attachment_says_what_it_actually_found(self, tmp_path: Path):
        # "No original PDF attachment found" told the operator nothing about
        # why. The archive holds the KSeF XML — the message has to say so, or
        # the same three months repeat.
        client = self._client(_archive("7341173780-20260731-2EFE5D800001-64.xml"))

        with pytest.raises(RuntimeError, match=r"(?i)ksef"):
            client.download_original_attachments(1, tmp_path, filename_prefix="x")

    def test_a_pdf_attachment_is_still_taken(self, tmp_path: Path):
        client = self._client(_archive("faktura.pdf"))

        saved = client.download_original_attachments(1, tmp_path, filename_prefix="x")

        assert len(saved) == 1
        assert "__original__" in saved[0].name

    def test_an_empty_archive_is_reported_as_empty_not_as_ksef(self, tmp_path: Path):
        client = self._client(_archive("notes.txt"))

        with pytest.raises(RuntimeError, match=r"(?i)brak|no "):
            client.download_original_attachments(1, tmp_path, filename_prefix="x")


class TestTheFallbackIsRecordedNotWhispered:
    def test_falling_back_to_a_generated_pdf_leaves_a_reason(self, tmp_path: Path):
        # The operator has to be able to tell "no original exists" apart from
        # "an original exists and we could not read it".
        client = FakturowniaClient.__new__(FakturowniaClient)
        client.pdf_delay = 0
        client.download_original_attachments = MagicMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("Original is a KSeF XML, not a PDF")
        )
        client.download_pdf = MagicMock(  # type: ignore[method-assign]
            side_effect=lambda _id, path: path.write_bytes(b"pdf") or path
        )

        invoices = [{"id": 1, "number": "FV/1/2026", "buyer_name": "X", "has_attachments": True}]
        with patch.object(FakturowniaClient, "cost_document_stem", staticmethod(lambda inv: "x_1")):
            saved = client.download_cost_documents(invoices, tmp_path)

        assert len(saved) == 1
        assert saved[0].source_kind == "generated_pdf"
        assert saved[0].fallback_reason is not None
        assert "ksef" in saved[0].fallback_reason.casefold()

    def test_a_document_with_no_original_at_all_has_no_reason(self, tmp_path: Path):
        client = FakturowniaClient.__new__(FakturowniaClient)
        client.pdf_delay = 0
        client.download_pdf = MagicMock(  # type: ignore[method-assign]
            side_effect=lambda _id, path: path.write_bytes(b"pdf") or path
        )

        invoices = [{"id": 2, "number": "FV/2/2026", "buyer_name": "X", "has_attachments": False}]
        with patch.object(FakturowniaClient, "cost_document_stem", staticmethod(lambda inv: "x_2")):
            saved = client.download_cost_documents(invoices, tmp_path)

        assert saved[0].source_kind == "generated_pdf"
        assert saved[0].fallback_reason is None


class TestTheFixReachesTheCodeThatMatters:
    """A helper nobody calls fixes nothing."""

    def test_the_inspector_matches_the_accountant_despite_the_polish_l(self):
        from zdrovena.month_closing import inspection

        vendor = inspection._find_vendor(
            {"buyer_name": "BIURO RACHUNKOWE MGR BOŻENA OGORZAŁEK", "buyer_tax_no": ""}
        )

        assert vendor is not None and vendor.name == "Accounting/Bożena"

    def test_neither_matcher_reimplements_the_comparison(self):
        # Two hand-rolled substring checks are how the diacritic bug survived
        # in one place while being fixed in another.
        for path in (
            "zdrovena/month_closing/workflow.py",
            "zdrovena/month_closing/orchestrator.py",
        ):
            source = Path(path).read_text(encoding="utf-8")
            assert "pattern.casefold() in buyer" not in source, path
