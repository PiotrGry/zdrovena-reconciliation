"""Unattended JPK_V7M from Fakturownia's accounting module (PoC E3, zdrovena#583).

The path was checked on the test account on 2026-11-20:

1. `/accounting/app/reports/jpk_vat` shows the month (the previous month by default) and "Nowy raport".
2. "Nowy raport" creates a report on Ksiegosoft's server (`/jpk_vat/<id>`).
3. The taxpayer data must be filled: tax office, first name, last name, date of birth and e-mail.
4. The statement "potwierdzam zapoznanie się z treścią i akceptację pouczeń" must be ticked. The
   operator decided on 2026-11-20 that the job ticks it.
5. "zapisz i generuj xml" generates the XML, and "pobierz xml" downloads it (a presigned S3 link).

"Wyślij do Ministerstwa Finansów" is never clicked: it is not on the allowlist, and the guard disables it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from zdrovena.month_closing import fakturownia_reports as fr

logger = logging.getLogger(__name__)

TAXPAYER_ENV = "FAKTUROWNIA_V7_TAXPAYER"
_MONTHS_PL = (
    "styczeń", "luty", "marzec", "kwiecień", "maj", "czerwiec",
    "lipiec", "sierpień", "wrzesień", "październik", "listopad", "grudzień",
)  # fmt: skip


@dataclass(frozen=True)
class V7Taxpayer:
    tax_office: str
    first_name: str
    last_name: str
    birth_date: str
    email: str


def load_taxpayer(environ: dict[str, str] | None = None) -> V7Taxpayer:
    """Taxpayer data for the V7 form, as JSON in FAKTUROWNIA_V7_TAXPAYER. Raises ValueError."""
    raw = (environ if environ is not None else os.environ).get(TAXPAYER_ENV, "").strip()
    if not raw:
        raise ValueError(f"{TAXPAYER_ENV} is not set")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{TAXPAYER_ENV} is not valid JSON") from exc
    fields = ("tax_office", "first_name", "last_name", "birth_date", "email")
    missing = [name for name in fields if not str(data.get(name, "")).strip()]
    if missing:
        raise ValueError(f"{TAXPAYER_ENV} lacks {', '.join(missing)}")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", data["birth_date"]):
        raise ValueError("birth_date must be yyyy-mm-dd")
    if not fr.V7_TAX_OFFICE_OPTION.label_re.search(data["tax_office"]):
        raise ValueError("tax_office must be the option text, e.g. 'URZĄD SKARBOWY W BOLESŁAWCU'")
    return V7Taxpayer(**{name: str(data[name]).strip() for name in fields})


def month_label(month: str) -> str:
    """'2026-10' → 'październik 2026', as the wizard's "Za miesiąc" field shows it."""
    year, mon = (int(part) for part in month.split("-"))
    return f"{_MONTHS_PL[mon - 1]} {year}"


def check_v7m_xml(data: bytes, month: str) -> str | None:
    """Why ``data`` is not the JPK_V7M of ``month`` (an original filing), or None."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return "not an XML document"
    found = {el.tag.rsplit("}", 1)[-1]: el for el in root.iter()}
    code = found.get("KodFormularza")
    if code is None or not (code.get("kodSystemowy") or "").startswith("JPK_V7M"):
        return "not a JPK_V7M file"
    year, mon = month.split("-")
    if (found.get("Rok") is None or found["Rok"].text != year) or (
        found.get("Miesiac") is None or int(found["Miesiac"].text or 0) != int(mon)
    ):
        return f"file is not for {month}"
    if found.get("CelZlozenia") is None or found["CelZlozenia"].text != "1":
        return "not an original filing (CelZlozenia != 1)"
    return None


def _wait_visible(page, locator_fn, timeout_ms: int):
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        found = fr._first_visible(locator_fn())
        if found is not None:
            return found
        page.wait_for_timeout(300)
    return None


def _enabled(locator) -> bool:
    try:
        return locator.is_enabled()
    except Exception:
        return False


def run_v7m_report(
    page,
    month: str,
    output_dir: Path,
    *,
    base_url: str,
    taxpayer: V7Taxpayer,
    job_wait_ms: int = 120_000,
    step_timeout_ms: int = 15_000,
    dest_name: str = "JPK_V7M.xml",
) -> Path:
    """Create, generate and download the JPK_V7M of ``month``. Raises fr.ReportFlowError."""
    base = base_url.rstrip("/")
    output_path = output_dir / dest_name
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        page.goto(f"{base}/accounting/app/reports/jpk_vat", wait_until="domcontentloaded")
        new_report = _wait_visible(
            page, lambda: page.get_by_role("button", name="Nowy raport"), job_wait_ms
        )
        if new_report is None:
            raise fr.ReportControlMissing(fr.V7_NEW_REPORT.name)
        shown = page.get_by_label("Za miesiąc").first.input_value(timeout=step_timeout_ms)
        if shown.strip().lower() != month_label(month):
            # The month picker was not checked by hand; the default (previous month) is what month close needs.
            raise fr.ReportFlowError(f"the wizard shows {shown!r}, not {month_label(month)!r}")
        fr._click_allowlisted(new_report, fr.V7_NEW_REPORT)
        page.wait_for_url(re.compile(r"/accounting/app/reports/jpk_vat/\d+"), timeout=job_wait_ms)
        generate = _wait_visible(
            page, lambda: page.get_by_role("button", name="zapisz i generuj xml"), job_wait_ms
        )
        if generate is None:
            raise fr.ReportControlMissing(fr.V7_GENERATE_XML.name)
        logger.info("JPK_V7M: report %s created", page.url.rsplit("/", 1)[-1].split("?")[0])

        # Taxpayer data: fill only what is empty (a production account may keep it).
        if not page.get_by_text(taxpayer.tax_office, exact=True).first.is_visible():
            # The label, not the bare input: the input has no text, and the allowlist checks text.
            field = page.get_by_text("Urząd skarbowy", exact=True).first
            fr._click_allowlisted(field, fr.V7_TAX_OFFICE_FIELD, force=True)
            option = _wait_visible(
                page, lambda: page.get_by_text(taxpayer.tax_office, exact=True), step_timeout_ms
            )
            if option is None:
                raise fr.ReportControlMissing(fr.V7_TAX_OFFICE_OPTION.name, taxpayer.tax_office)
            fr._click_allowlisted(option, fr.V7_TAX_OFFICE_OPTION, force=True)
        for label, value in (
            ("Imię", taxpayer.first_name),
            ("Nazwisko", taxpayer.last_name),
            ("Data urodzenia", taxpayer.birth_date),
            ("Adres email", taxpayer.email),
        ):
            box = page.get_by_label(label).first
            if not box.input_value(timeout=step_timeout_ms).strip():
                box.fill(value, timeout=step_timeout_ms)

        # The statement last: the app validates the form when it is ticked. A tick on an
        # already-ticked box unticks it, so toggle until the generate button is enabled.
        statement = page.get_by_text(fr.V7_STATEMENT.label_re).first
        for _ in range(2):
            if _enabled(generate):
                break
            fr._click_allowlisted(statement, fr.V7_STATEMENT, force=True)
            page.wait_for_timeout(800)
        if not _enabled(generate):
            raise fr.ReportFlowError(
                "'zapisz i generuj xml' stays disabled: a required field is missing"
            )
        fr._click_allowlisted(generate, fr.V7_GENERATE_XML, force=True)

        download_button = _wait_visible(
            page, lambda: page.get_by_role("button", name="pobierz xml"), job_wait_ms
        )
        if download_button is None:
            raise fr.ReportControlMissing(fr.V7_DOWNLOAD_XML.name, "not shown after generating")
        # A click while the page re-renders after generating is lost (no request, PoC E3 run
        # 20261120T194552Z). Wait until the button is enabled, click without force, and try once
        # more: the control only downloads.
        download = None
        for attempt in range(2):
            deadline = time.monotonic() + step_timeout_ms / 1000
            while not _enabled(download_button) and time.monotonic() < deadline:
                page.wait_for_timeout(300)
            page.wait_for_timeout(500)
            try:
                with page.expect_download(timeout=step_timeout_ms) as info:
                    fr._click_allowlisted(download_button, fr.V7_DOWNLOAD_XML)
                download = info.value
                break
            except Exception as exc:
                logger.warning(
                    "JPK_V7M: no download after 'pobierz xml' (attempt %d): %s", attempt + 1, exc
                )
        if download is None:
            raise fr.ReportControlMissing(fr.V7_DOWNLOAD_XML.name, "clicked twice, no download")
        part = output_path.with_name(output_path.name + ".part")
        download.save_as(str(part))
        problem = check_v7m_xml(part.read_bytes(), month)
        if problem:
            part.unlink(missing_ok=True)
            raise fr.ReportFlowError(f"downloaded {download.suggested_filename!r}: {problem}")
        os.replace(part, output_path)
    except fr.ReportFlowError:
        fr._screenshot_beside(page, output_path)
        raise
    except Exception as exc:
        shot = fr._screenshot_beside(page, output_path)
        raise fr.ReportFlowError(f"JPK_V7M: {exc}; screenshot: {shot}") from exc
    logger.info("JPK_V7M: saved %s (%d bytes)", output_path, output_path.stat().st_size)
    return output_path
