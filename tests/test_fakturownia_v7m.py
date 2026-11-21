import json

import pytest

from zdrovena.month_closing import fakturownia_reports as fr
from zdrovena.month_closing import fakturownia_v7m as v7

TAXPAYER = {
    "tax_office": "URZĄD SKARBOWY W BOLESŁAWCU",
    "first_name": "Jan",
    "last_name": "Testowy",
    "birth_date": "1980-01-01",
    "email": "jan.testowy@example.com",
}


def _xml(*, code="JPK_V7M (3)", year="2026", month="10", purpose="1") -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<tns:JPK xmlns:tns="http://crd.gov.pl/wzor/2021/12/27/11148/">
  <tns:Naglowek>
    <tns:KodFormularza kodSystemowy="{code}" wersjaSchemy="1-0E">JPK_VAT</tns:KodFormularza>
    <tns:CelZlozenia poz="P_7">{purpose}</tns:CelZlozenia>
    <tns:Rok>{year}</tns:Rok>
    <tns:Miesiac>{month}</tns:Miesiac>
  </tns:Naglowek>
</tns:JPK>""".encode()


def test_taxpayer_is_read_from_json_and_validated():
    taxpayer = v7.load_taxpayer({v7.TAXPAYER_ENV: json.dumps(TAXPAYER)})
    assert taxpayer.tax_office == "URZĄD SKARBOWY W BOLESŁAWCU"
    for broken in (
        {},
        {**TAXPAYER, "email": " "},
        {**TAXPAYER, "birth_date": "01.01.1980"},
        {**TAXPAYER, "tax_office": "Bolesławiec"},
    ):
        with pytest.raises(ValueError):
            v7.load_taxpayer({v7.TAXPAYER_ENV: json.dumps(broken)})
    with pytest.raises(ValueError, match="not set"):
        v7.load_taxpayer({})
    with pytest.raises(ValueError, match="not valid JSON"):
        v7.load_taxpayer({v7.TAXPAYER_ENV: "{"})


@pytest.mark.parametrize(
    ("month", "label"),
    [("2026-10", "październik 2026"), ("2027-01", "styczeń 2027"), ("2026-12", "grudzień 2026")],
)
def test_month_label_matches_the_wizard(month, label):
    assert v7.month_label(month) == label


def test_downloaded_xml_must_be_the_v7m_of_the_month_and_an_original_filing():
    assert v7.check_v7m_xml(_xml(), "2026-10") is None
    assert v7.check_v7m_xml(_xml(month="9"), "2026-10") == "file is not for 2026-10"
    assert v7.check_v7m_xml(_xml(year="2025"), "2026-10") == "file is not for 2026-10"
    assert "CelZlozenia" in v7.check_v7m_xml(_xml(purpose="2"), "2026-10")
    assert v7.check_v7m_xml(_xml(code="JPK_FA (4)"), "2026-10") == "not a JPK_V7M file"
    assert v7.check_v7m_xml(b"<html>login</html", "2026-10") == "not an XML document"


@pytest.mark.parametrize(
    ("control", "label"),
    [
        (fr.V7_NEW_REPORT, "Nowy raport"),
        (fr.V7_TAX_OFFICE_FIELD, "Urząd skarbowy"),
        (fr.V7_TAX_OFFICE_OPTION, "URZĄD SKARBOWY W BOLESŁAWCU"),
        (fr.V7_STATEMENT, "potwierdzam zapoznanie się z treścią i akceptacje poniższych pouczeń *"),
        (fr.V7_GENERATE_XML, "zapisz i generuj xml"),
        (fr.V7_DOWNLOAD_XML, "pobierz xml"),
    ],
)
def test_v7_controls_match_the_labels_seen_on_the_test_account(control, label):
    assert control in fr.ALLOWED_CONTROLS
    assert control.label_re.search(label)


def test_the_send_link_seen_after_generating_is_never_clickable():
    label = "Wyślij do Ministerstwa Finansów"
    assert fr.is_send_control_text(label)
    assert not any(c.label_re and c.label_re.search(label) for c in fr.ALLOWED_CONTROLS)


def test_print_pdf_starts_the_vat_job_and_is_never_the_export_fallback():
    assert fr.PRINT_PDF.label_re.search("Drukuj do PDF")
    assert not fr.VAT_REGISTER_DOWNLOAD.label_re.search("Drukuj do PDF")
    assert fr.VAT_REGISTER_DOWNLOAD.label_re.search("Pobierz")
