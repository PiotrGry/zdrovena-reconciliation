"""
zdrovena.month_closing.invoice_date_check – Invoice Date Validation
=====================================================================
Extracts issue dates from downloaded PDF invoices and validates whether
they fall within the expected closing month.

Also provides text-based deduplication and OCR fallback via Tesseract.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("zdrovena.month_closing.invoice_date_check")

_OCR_CACHE: dict[str, str] = {}

_PL_MONTHS = {
    "sty": 1,
    "stycz": 1,
    "stycznia": 1,
    "styczeń": 1,
    "lut": 2,
    "luty": 2,
    "lutego": 2,
    "mar": 3,
    "marz": 3,
    "marca": 3,
    "marzec": 3,
    "kwi": 4,
    "kwiet": 4,
    "kwietnia": 4,
    "kwiecień": 4,
    "maj": 5,
    "maja": 5,
    "cze": 6,
    "czerw": 6,
    "czerwca": 6,
    "czerwiec": 6,
    "lip": 7,
    "lipca": 7,
    "lipiec": 7,
    "sie": 8,
    "sierp": 8,
    "sierpnia": 8,
    "sierpień": 8,
    "wrz": 9,
    "wrześ": 9,
    "września": 9,
    "wrzesień": 9,
    "paź": 10,
    "pazd": 10,
    "października": 10,
    "październik": 10,
    "lis": 11,
    "listop": 11,
    "listopada": 11,
    "listopad": 11,
    "gru": 12,
    "grudz": 12,
    "grudnia": 12,
    "grudzień": 12,
}

_EN_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}

_ALL_MONTHS = {**_PL_MONTHS, **_EN_MONTHS}

_NUMERIC_DATE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"Data\s+wystawienia\s*[:\-–]\s*(\d{4}[.\-/]\d{2}[.\-/]\d{2})", re.IGNORECASE),
        "%Y-%m-%d",
    ),
    (
        re.compile(r"Data\s+wystawienia\s*[:\-–]\s*(\d{2}[.\-/]\d{2}[.\-/]\d{4})", re.IGNORECASE),
        "%d-%m-%Y",
    ),
    (
        re.compile(
            r"(\d{4}[.\-/]\d{2}[.\-/]\d{2})\s*\n\s*(?:\d{4}[.\-/]\d{2}[.\-/]\d{2}\s*\n\s*)*Data\s+wystawienia",
            re.IGNORECASE,
        ),
        "%Y-%m-%d",
    ),
    (
        re.compile(
            r"data\s+wystawien\w*\s*[:]\s*[A-ZŁŚŻa-złśż\s]+(\d{2}[.\-/]\d{2}[.\-/]\d{4})",
            re.IGNORECASE,
        ),
        "%d-%m-%Y",
    ),
    (
        re.compile(
            r"Wystawion[ay]\s+w\s+dniu\s*[:\-–]\s*(\d{4}[.\-/]\d{2}[.\-/]\d{2})", re.IGNORECASE
        ),
        "%Y-%m-%d",
    ),
    (
        re.compile(
            r"Wystawion[ay]\s+w\s+dniu\s*[:\-–]\s*(\d{2}[.\-/]\d{2}[.\-/]\d{4})", re.IGNORECASE
        ),
        "%d-%m-%Y",
    ),
    (
        re.compile(r"Data\s+faktury\s*[:\-–.\s]*(\d{4}[.\-/]\d{2}[.\-/]\d{2})", re.IGNORECASE),
        "%Y-%m-%d",
    ),
    (
        re.compile(r"Data\s+faktury\s*[:\-–.\s]*(\d{2}[.\-/]\d{2}[.\-/]\d{4})", re.IGNORECASE),
        "%d-%m-%Y",
    ),
    (
        re.compile(r"Data\s+wydruku\s*[:\-–]\s*(\d{4}[.\-/]\d{2}[.\-/]\d{2})", re.IGNORECASE),
        "%Y-%m-%d",
    ),
    (
        re.compile(r"Data\s+wydruku\s*[:\-–]\s*(\d{2}[.\-/]\d{2}[.\-/]\d{4})", re.IGNORECASE),
        "%d-%m-%Y",
    ),
    (re.compile(r"PEKAO[^\n]*\n(\d{4}[.\-/]\d{2}[.\-/]\d{2})", re.IGNORECASE), "%Y-%m-%d"),
    (
        re.compile(
            r"\d{2}\s\d{4}\s\d{4}\s\d{4}\s\d{4}\s\d{4}\s\d{4}\s*\n(\d{4}[.\-/]\d{2}[.\-/]\d{2})"
        ),
        "%Y-%m-%d",
    ),
    (
        re.compile(r"Data\s+sprzeda[żz]y\s*[:\-–]\s*(\d{4}[.\-/]\d{2}[.\-/]\d{2})", re.IGNORECASE),
        "%Y-%m-%d",
    ),
    (
        re.compile(r"Data\s+sprzeda[żz]y\s*[:\-–]\s*(\d{2}[.\-/]\d{2}[.\-/]\d{4})", re.IGNORECASE),
        "%d-%m-%Y",
    ),
    (
        re.compile(
            r"(?:Issue|Invoice)\s+[Dd]ate\s*[:\-–]\s*(\d{4}[.\-/]\d{2}[.\-/]\d{2})", re.IGNORECASE
        ),
        "%Y-%m-%d",
    ),
    (
        re.compile(
            r"(?:Issue|Invoice)\s+[Dd]ate\s*[:\-–]\s*(\d{2}[.\-/]\d{2}[.\-/]\d{4})", re.IGNORECASE
        ),
        "%d-%m-%Y",
    ),
    (
        re.compile(r"(\d{2}[.\-/]\d{2}[.\-/]\d{4})\s*\n\s*date\s+of\s+issue", re.IGNORECASE),
        "%d-%m-%Y",
    ),
    (
        re.compile(r"(\d{4}[.\-/]\d{2}[.\-/]\d{2})\s*\n\s*date\s+of\s+issue", re.IGNORECASE),
        "%Y-%m-%d",
    ),
]

_MONTH_NAMES_RE = "|".join(re.escape(m) for m in sorted(_ALL_MONTHS.keys(), key=len, reverse=True))

_TEXT_MONTH_PATTERNS: list[re.Pattern[str]] = [
    re.compile(rf"\b(\d{{1,2}})\s+({_MONTH_NAMES_RE})\s+(\d{{4}})\b", re.IGNORECASE),
    re.compile(rf"\b({_MONTH_NAMES_RE})\s+(\d{{1,2}}),?\s+(\d{{4}})\b", re.IGNORECASE),
]

_FALLBACK_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"Faktura[^\n]*\n\s*(\d{4}[.\-/]\d{2}[.\-/]\d{2})", re.IGNORECASE), "%Y-%m-%d"),
]

_INVOICE_NUMBER_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"Faktura\s+(?:VAT\s+)?(?:Nr[:\s]*)?(\S+/\S+)", re.IGNORECASE),
    re.compile(r"FAKTURA\s+VAT\s+Nr\s*[:\-–]\s*(\S+)", re.IGNORECASE),
    re.compile(r"FAKTURA\s+VAT[^\n]*\bnr\s+(\S+/\S+)", re.IGNORECASE),
    re.compile(r"Invoice\s+no\.?\s*\n?\s*(\S+)", re.IGNORECASE),
    re.compile(r"Numer\s+faktury\s*[:\n]\s*(\S+)", re.IGNORECASE),
    re.compile(r"Bill\s+#\s*(\d+)", re.IGNORECASE),
]


def _normalize_date_sep(s: str) -> str:
    return s.replace(".", "-").replace("/", "-")


def _parse_text_month_date(match: re.Match[str], pattern_idx: int) -> date | None:
    try:
        if pattern_idx == 0:
            day_s, month_s, year_s = match.group(1), match.group(2), match.group(3)
        else:
            month_s, day_s, year_s = match.group(1), match.group(2), match.group(3)
        month_num = _ALL_MONTHS.get(month_s.lower())
        if not month_num:
            return None
        return date(int(year_s), month_num, int(day_s))
    except (ValueError, TypeError):
        return None


def _ocr_fallback(pdf_path: Path) -> str:
    """Last-resort OCR via Tesseract for scanned PDFs."""
    import shutil

    cache_key = str(pdf_path.resolve())
    if cache_key in _OCR_CACHE:
        return _OCR_CACHE[cache_key]

    if not shutil.which("tesseract"):
        return ""

    ocr_text = ""
    try:
        from pypdf import PdfReader

        reader = PdfReader(pdf_path)
        images_found = False
        for page_idx, page in enumerate(reader.pages):
            if not hasattr(page, "images") or not page.images:
                continue
            images_found = True
            for img in page.images:
                ocr_text += _ocr_image(img, pdf_path.name, page_idx) + "\n"
        if not images_found and len(reader.pages) > 0:
            ocr_text += _ocr_rendered_pdf(pdf_path)
    except ImportError:
        return ""
    except Exception as exc:
        logger.warning("OCR preprocessing failed for %s: %s", pdf_path.name, exc)

    if ocr_text.strip():
        logger.info("  OCR (Tesseract) extracted %d chars from %s", len(ocr_text), pdf_path.name)
    _OCR_CACHE[cache_key] = ocr_text
    return ocr_text


def _ocr_image(img: Any, pdf_name: str, page_idx: int) -> str:
    import tempfile
    from pathlib import Path as _Path

    suffix = ".jpg" if hasattr(img, "name") and img.name.lower().endswith(".jpg") else ".png"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(img.data)
        tmp_path = tmp.name
    try:
        return _run_tesseract(tmp_path, pdf_name, page_idx)
    finally:
        _Path(tmp_path).unlink(missing_ok=True)


def _ocr_rendered_pdf(pdf_path: Path) -> str:
    import tempfile
    from pathlib import Path as _Path

    try:
        from pdf2image import convert_from_path
    except ImportError:
        return ""
    ocr_text = ""
    try:
        images = convert_from_path(str(pdf_path), first_page=1, last_page=5)
        for page_idx, image in enumerate(images):
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                image.save(tmp.name)
                tmp_path = tmp.name
            try:
                ocr_text += _run_tesseract(tmp_path, pdf_path.name, page_idx) + "\n"
            finally:
                _Path(tmp_path).unlink(missing_ok=True)
    except Exception as exc:
        logger.warning("PDF rendering failed for %s: %s", pdf_path.name, exc)
    return ocr_text


def _run_tesseract(image_path: str, pdf_name: str, page_idx: int) -> str:
    import subprocess

    ocr_text = ""
    for psm_mode in [6, 3]:
        try:
            result = subprocess.run(
                ["tesseract", image_path, "-", "-l", "osd+pol+eng", "--psm", str(psm_mode)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0 and result.stdout.strip():
                ocr_text = result.stdout.strip()
                break
        except subprocess.TimeoutExpired:
            logger.warning("  Tesseract timeout on page %d of %s", page_idx, pdf_name)
        except FileNotFoundError:
            break
    return ocr_text


def extract_text(pdf_path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    try:
        reader = PdfReader(pdf_path)
        text = ""
        for page in reader.pages:
            page_text = page.extract_text()
            if page_text:
                text += page_text + "\n"
        if text.strip():
            return text
        return _ocr_fallback(pdf_path)
    except Exception as exc:
        logger.warning("Cannot read PDF %s: %s", pdf_path.name, exc)
        return ""


def extract_issue_date(pdf_path: Path, text: str | None = None) -> date | None:
    if text is None:
        text = extract_text(pdf_path)
    if not text.strip():
        return None

    for pattern, fmt in _NUMERIC_DATE_PATTERNS:
        m = pattern.search(text)
        if m:
            raw = _normalize_date_sep(m.group(1))
            try:
                dt = datetime.strptime(raw, fmt)
                return dt.date()
            except ValueError:
                continue

    for idx, pattern in enumerate(_TEXT_MONTH_PATTERNS):
        m = pattern.search(text)
        if m:
            d = _parse_text_month_date(m, idx)
            if d:
                return d

    for pattern, fmt in _FALLBACK_PATTERNS:
        m = pattern.search(text)
        if m:
            raw = _normalize_date_sep(m.group(1))
            try:
                dt = datetime.strptime(raw, fmt)
                return dt.date()
            except ValueError:
                continue

    return None


def extract_invoice_number(pdf_path: Path, text: str | None = None) -> str | None:
    if text is None:
        text = extract_text(pdf_path)
    if not text.strip():
        return None
    for pattern in _INVOICE_NUMBER_PATTERNS:
        m = pattern.search(text)
        if m:
            inv_num = m.group(1).strip().rstrip(",.")
            if len(inv_num) >= 3:
                return inv_num
    return None


def is_likely_invoice(pdf_path: Path, text: str | None = None) -> bool:
    if text is None:
        text = extract_text(pdf_path)
    if not text.strip():
        return True
    text_lower = text.lower()
    strong_invoice_keywords = ["faktura", "invoice", "rachunek", "bill #"]
    has_strong_invoice_keyword = any(kw in text_lower for kw in strong_invoice_keywords)
    if not has_strong_invoice_keyword:
        return False
    non_invoice_negators = [
        "proforma",
        "oświadczenie",
        "wyborze formy",
        "zeznanie",
        "wykaz sprzedaży",
        "wyciąg",
    ]
    return not any(neg in text_lower for neg in non_invoice_negators)


def validate_invoice_dates(
    saved_paths: list[Path],
    month_start: date,
    month_end: date,
    strict: bool = True,
) -> tuple[list[Path], list[Path], list[Path]]:
    accepted: list[Path] = []
    rejected: list[Path] = []
    rejected_reasons: dict[str, str] = {}
    unverified: list[Path] = []
    seen_invoice_numbers: dict[str, Path] = {}

    for pdf_path in saved_paths:
        if not pdf_path.exists():
            continue
        text = extract_text(pdf_path)
        if text.strip() and not is_likely_invoice(pdf_path, text):
            rejected.append(pdf_path)
            rejected_reasons[pdf_path.name] = "not an invoice"
            continue
        inv_num = extract_invoice_number(pdf_path, text)
        if inv_num:
            if inv_num in seen_invoice_numbers:
                first = seen_invoice_numbers[inv_num]
                rejected.append(pdf_path)
                rejected_reasons[pdf_path.name] = f"duplicate of {first.name}"
                continue
            seen_invoice_numbers[inv_num] = pdf_path
        issue_date = extract_issue_date(pdf_path, text)
        if issue_date is None:
            if strict:
                unverified.append(pdf_path)
            else:
                # Date unreadable but file came from Zoho (already date-filtered) — accept.
                accepted.append(pdf_path)
        elif month_start <= issue_date <= month_end:
            accepted.append(pdf_path)
        else:
            rejected.append(pdf_path)
            rejected_reasons[pdf_path.name] = f"wrong date ({issue_date.isoformat()})"

    if accepted:
        logger.info("✅ ACCEPTED (%d invoice(s)):", len(accepted))
        for p in accepted:
            logger.info("  ✅ %s", p.name)
    if rejected:
        logger.info("❌ REJECTED (%d file(s)):", len(rejected))
        for p in rejected:
            reason = rejected_reasons.get(p.name, "unknown reason")
            logger.info("  ❌ %s — %s", p.name, reason)
    if unverified:
        logger.info("⚠️  UNVERIFIED (%d file(s) — no date):", len(unverified))
        for p in unverified:
            logger.info("  ⚠️  %s", p.name)

    return accepted, rejected, unverified


def delete_rejected(rejected: list[Path]) -> list[Path]:
    deleted: list[Path] = []
    for pdf_path in rejected:
        try:
            pdf_path.unlink()
            deleted.append(pdf_path)
        except OSError as exc:
            logger.warning("  Failed to delete %s: %s", pdf_path.name, exc)
    return deleted


def move_unverified(unverified: list[Path], label: str = "_manual_check") -> list[Path]:
    moved: list[Path] = []
    for pdf_path in unverified:
        dest_dir = pdf_path.parent / label
        dest_dir.mkdir(exist_ok=True)
        dest = dest_dir / pdf_path.name
        try:
            pdf_path.rename(dest)
            moved.append(dest)
        except OSError as exc:
            logger.warning("  Failed to move %s: %s", pdf_path.name, exc)
    return moved
