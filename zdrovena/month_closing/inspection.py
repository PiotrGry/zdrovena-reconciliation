"""Read-only inspection of one accounting period.

Answers a single question — *how does this period stand right now?* — by
listing what is in storage and what Fakturownia holds, without writing a file,
sending a message or touching the close run.

Kept apart from the workflow on purpose. The workflow is the machine of steps,
gates and waivers; this is the picture it looks at. Separating them is what
lets an operator ask about a period without moving it through a state machine,
and it is the foundation the rebuilt month close grows from.
"""

from __future__ import annotations

import calendar
import fnmatch
from pathlib import Path
from typing import Any

from zdrovena.audit.sections import check_numbering
from zdrovena.common import FakturowniaClient
from zdrovena.common.storage import StorageService, get_storage_service
from zdrovena.month_closing.config import (
    BASE_DIR,
    EXPECTED_VENDORS,
    FAKTUROWNIA_REPORTS,
    POLISH_MONTHS,
    VendorConfig,
)
from zdrovena.month_closing.preflight import pko_matches_month
from zdrovena.month_closing.warehouse_audit import warehouse_issues


def build_document(
    document_id: str,
    category: str,
    label: str,
    status: str,
    *,
    required: bool = True,
    source: str | None = None,
    file_key: str | None = None,
    message: str | None = None,
) -> dict[str, Any]:
    return {
        "id": document_id,
        "category": category,
        "label": label,
        "status": status,
        "required": required,
        "source": source,
        "file_key": file_key,
        "message": message,
    }


def _sales_pdf_name(invoice_number: str) -> str:
    """Filename ``FakturowniaClient.download_all_pdfs`` writes for an invoice.

    Kept in lockstep with that method — if the sanitising there changes, the
    package gate below silently stops matching.
    """
    safe = invoice_number.replace("/", "_").replace("\\", "_").replace(" ", "_")
    return f"{safe}.pdf"


def build_issue(
    issue_id: str,
    severity: str,
    message: str,
    *,
    stage: str = "check",
) -> dict[str, str]:
    return {
        "id": issue_id,
        "severity": severity,
        "message": message,
        "stage": stage,
    }


def _find_vendor(inv: dict[str, Any]) -> VendorConfig | None:
    buyer = (inv.get("buyer_name") or "").casefold()
    buyer_nip = (inv.get("buyer_tax_no") or "").casefold()
    return next(
        (
            vendor
            for vendor in EXPECTED_VENDORS
            if not vendor.skip
            and (vendor.pattern.casefold() in buyer or vendor.pattern.casefold() in buyer_nip)
        ),
        None,
    )


class MonthCloseInspector:
    """Build the dashboard state without writing files or sending messages."""

    def __init__(
        self,
        year: int,
        month: int,
        *,
        storage: StorageService | None = None,
    ) -> None:
        self.year = year
        self.month = month
        self.storage = storage or get_storage_service()
        self.month_pl = POLISH_MONTHS[month]
        self.month_dir = BASE_DIR / str(year) / self.month_pl
        self.month_prefix = f"faktury/{year}/{self.month_pl}"
        self.inbox_prefix = f"faktury/inbox/{year:04d}-{month:02d}"
        last_day = calendar.monthrange(year, month)[1]
        self.date_from = f"{year:04d}-{month:02d}-01"
        self.date_to = f"{year:04d}-{month:02d}-{last_day:02d}"

    def inspect(self) -> dict[str, Any]:
        documents: list[dict[str, Any]] = []
        issues: list[dict[str, str]] = []
        inbox_files = self.storage.list_files(self.inbox_prefix.rstrip("/") + "/")
        month_files = self.storage.list_files(self.month_prefix.rstrip("/") + "/")
        inbox_by_name = {Path(item.key).name: item for item in inbox_files}
        month_by_name = {Path(item.key).name: item for item in month_files}

        documents.extend(self._manual_documents(inbox_by_name, month_by_name, issues))

        try:
            client = FakturowniaClient.from_keyring()
            sales = client.fetch_sales_invoices(self.date_from, self.date_to)
            costs = client.fetch_cost_invoices(self.date_from, self.date_to)
        except Exception as exc:
            documents.append(
                build_document(
                    "fakturownia",
                    "provider",
                    "Fakturownia",
                    "invalid",
                    source="Fakturownia API",
                    message="Nie udało się sprawdzić faktur.",
                )
            )
            issues.append(
                build_issue(
                    "fakturownia-unavailable",
                    "blocker",
                    f"Nie udało się sprawdzić Fakturowni: {exc}",
                )
            )
            return {
                "documents": documents,
                "issues": issues,
                "metrics": {"ready": False},
            }

        documents.append(
            build_document(
                "sales",
                "sales",
                "Faktury sprzedażowe",
                "available_automatically" if sales else "missing",
                source="Fakturownia API",
                message=f"{len(sales)} faktur w okresie" if sales else "Brak faktur w okresie.",
            )
        )
        if not sales:
            issues.append(
                build_issue(
                    "sales-missing", "blocker", "Brak faktur sprzedażowych za wybrany okres."
                )
            )

        # Guard the gap between "sales were collected" and "sales are still current".
        # The stage downloads a snapshot; Fakturownia keeps moving. If an invoice is
        # issued after collection, or two invoices shared a number and one PDF was
        # dropped, the package would quietly ship short. Only meaningful once
        # something has been collected — before that, step gating covers it.
        stored_pdfs = {
            Path(item.key).name
            for item in month_files
            if item.key.startswith(f"{self.month_prefix}/sprzedaz/")
            and item.key.lower().endswith(".pdf")
        }
        # Compare by name, not by count: a stray or obsolete PDF would otherwise
        # pad the total and hide a missing invoice. Names are derived exactly as
        # FakturowniaClient.download_all_pdfs() writes them.
        missing_pdfs = sorted(
            invoice_number
            for invoice_number in {str(inv.get("number", "")) for inv in sales}
            if invoice_number and _sales_pdf_name(invoice_number) not in stored_pdfs
        )
        # An empty folder is the normal state before `sales` runs, and step gating
        # already covers that. Only speak up once a collection exists to be wrong.
        if stored_pdfs and missing_pdfs:
            issues.append(
                build_issue(
                    "sales-pdfs-incomplete",
                    "blocker",
                    f"Brakuje PDF dla {len(missing_pdfs)} faktur "
                    f"({', '.join(missing_pdfs[:5])}) — uruchom etap sprzedaży ponownie.",
                    stage="sales",
                )
            )

        for series in check_numbering(sales):
            if series.gaps:
                issues.append(
                    build_issue(
                        f"sales-gaps-{series.series}",
                        "blocker",
                        f"Braki w numeracji /{series.series}: {series.gaps}",
                    )
                )
            if series.duplicates:
                issues.append(
                    build_issue(
                        f"sales-duplicates-{series.series}",
                        "blocker",
                        f"Duplikaty numeracji /{series.series}: {series.duplicates}",
                    )
                )

        # The warehouse side. Until #308 nothing in month-close asked whether an
        # invoice had a WZ behind it, so a month could be closed and mailed with
        # goods shipped and never billed, or billed and never shipped.
        issues.extend(warehouse_issues(client, self.year, self.month))

        documents.extend(self._cost_documents(costs, month_files, issues))
        return {
            "documents": documents,
            "issues": issues,
            "metrics": {
                "ready": not any(issue["severity"] == "blocker" for issue in issues),
                "sales_invoice_count": len(sales),
                "cost_invoice_count": len(costs),
                "original_cost_count": sum(bool(inv.get("has_attachments")) for inv in costs),
                "generated_cost_count": sum(not bool(inv.get("has_attachments")) for inv in costs),
            },
        }

    def _manual_documents(
        self,
        inbox_by_name: dict[str, Any],
        month_by_name: dict[str, Any],
        issues: list[dict[str, str]],
    ) -> list[dict[str, Any]]:
        documents: list[dict[str, Any]] = []
        all_names = {**inbox_by_name, **month_by_name}

        for report in FAKTUROWNIA_REPORTS:
            found_name = next(
                (
                    name
                    for name in all_names
                    if name == report["dest_name"] or fnmatch.fnmatch(name, report["glob"])
                ),
                None,
            )
            status = "present" if found_name else "missing"
            documents.append(
                build_document(
                    f"report-{report['name'].casefold().replace(' ', '-')}",
                    "reports",
                    report["name"],
                    status,
                    source="Wgrany plik" if found_name else "Fakturownia UI",
                    file_key=all_names[found_name].key if found_name else None,
                    message=found_name or "Pobierz raport i wgraj dla wybranego okresu.",
                )
            )
            if not found_name:
                issues.append(
                    build_issue(
                        f"report-missing-{report['name']}",
                        "blocker",
                        f"Brakuje raportu {report['name']}.",
                    )
                )

        bank_candidates = [
            name
            for name in all_names
            if name.casefold().endswith(".pdf")
            and ("wyciag" in name.casefold() or "pko" in name.casefold())
        ]
        period_tokens = {
            f"{self.year:04d}-{self.month:02d}",
            f"{self.year:04d}_{self.month:02d}",
            f"{self.year:04d}{self.month:02d}",
        }
        bank_name = next(
            (
                name
                for name in bank_candidates
                if pko_matches_month(name, self.year, self.month)
                or any(token in name for token in period_tokens)
            ),
            None,
        )
        invalid_bank_name = next((name for name in bank_candidates if name != bank_name), None)
        bank_status = "present" if bank_name else "invalid" if invalid_bank_name else "missing"
        documents.append(
            build_document(
                "bank-pko",
                "bank",
                "Wyciąg PKO BP",
                bank_status,
                source="Wgrany plik",
                file_key=(
                    all_names[bank_name].key
                    if bank_name
                    else all_names[invalid_bank_name].key
                    if invalid_bank_name
                    else None
                ),
                message=(
                    bank_name
                    or (
                        f"{invalid_bank_name} nie pasuje do wybranego okresu."
                        if invalid_bank_name
                        else "Wgraj wyciąg dla wybranego okresu."
                    )
                ),
            )
        )
        if not bank_name:
            issues.append(
                build_issue(
                    "bank-missing",
                    "blocker",
                    (
                        f"Wyciąg {invalid_bank_name} nie pasuje do okresu "
                        f"{self.year}-{self.month:02d}."
                        if invalid_bank_name
                        else f"Brakuje wyciągu PKO BP za {self.year}-{self.month:02d}."
                    ),
                )
            )

        for vendor in EXPECTED_VENDORS:
            if not vendor.download_glob:
                continue
            found_name = next(
                (
                    name
                    for name in all_names
                    if fnmatch.fnmatch(name, vendor.download_glob or "")
                    or fnmatch.fnmatch(
                        name,
                        f"{vendor.name.replace(' ', '_')}_{vendor.download_glob or ''}",
                    )
                ),
                None,
            )
            documents.append(
                build_document(
                    f"manual-{vendor.name.casefold().replace(' ', '-')}",
                    "costs",
                    f"{vendor.name} — dokument ręczny",
                    "present" if found_name else "missing",
                    source="Wgrany plik",
                    file_key=all_names[found_name].key if found_name else None,
                    message=found_name or "Wymaga ręcznego pobrania lub wgrania.",
                )
            )
            if not found_name:
                issues.append(
                    build_issue(
                        f"manual-missing-{vendor.name}",
                        "blocker",
                        f"Brakuje ręcznego dokumentu: {vendor.name}.",
                    )
                )
        return documents

    def _cost_documents(
        self,
        costs: list[dict[str, Any]],
        month_files: list[Any],
        issues: list[dict[str, str]],
    ) -> list[dict[str, Any]]:
        documents: list[dict[str, Any]] = []
        matched_vendors: set[str] = set()

        for inv in costs:
            vendor = _find_vendor(inv)
            if vendor:
                matched_vendors.add(vendor.name)
            number = str(inv.get("number") or inv.get("id"))
            label = f"{vendor.name if vendor else inv.get('buyer_name') or 'Koszt'} · {number}"
            has_original = bool(inv.get("has_attachments"))
            original_required = bool(vendor and vendor.source_policy == "original_required")
            stem = FakturowniaClient.cost_document_stem(inv).casefold()
            stored_original = next(
                (
                    item
                    for item in month_files
                    if Path(item.key).name.casefold().startswith(f"{stem}__original__")
                    and item.key.casefold().endswith(".pdf")
                ),
                None,
            )
            stored_generated = next(
                (item for item in month_files if Path(item.key).name.casefold() == f"{stem}.pdf"),
                None,
            )
            file_key = None
            if stored_original:
                status = "present"
                source = "Oryginalny załącznik Fakturowni"
                message = "Oryginalny PDF jest już w katalogu kosztów."
                file_key = stored_original.key
            elif original_required and stored_generated:
                status = "missing"
                source = "Wygenerowany PDF — do zastąpienia"
                message = "W katalogu jest render Fakturowni; wymagany jest oryginał."
                file_key = stored_generated.key
                issues.append(
                    build_issue(
                        f"generated-forbidden-{inv.get('id')}",
                        "blocker",
                        f"{label}: wygenerowany PDF musi zostać zastąpiony oryginałem.",
                    )
                )
            elif stored_generated:
                status = "present"
                source = "Wygenerowany PDF Fakturowni"
                message = "Dokument jest w katalogu, ale nie jest oryginalnym załącznikiem."
                file_key = stored_generated.key
                issues.append(
                    build_issue(
                        f"generated-stored-{inv.get('id')}",
                        "warning",
                        f"{label}: w paczce znajduje się oznaczony render Fakturowni.",
                    )
                )
            elif has_original:
                status = "available_automatically"
                source = "Oryginalny załącznik Fakturowni"
                message = "Oryginał zostanie pobrany automatycznie."
            elif original_required:
                status = "missing"
                source = "Zoho / wgranie ręczne"
                message = "Render Fakturowni jest zabroniony; wymagany jest oryginał."
                issues.append(
                    build_issue(
                        f"original-required-{inv.get('id')}",
                        "blocker",
                        f"{label}: wymagany jest oryginalny PDF.",
                    )
                )
            else:
                status = "available_automatically"
                source = "Wygenerowany PDF Fakturowni"
                message = "Brak załącznika; użyty będzie oznaczony render."
                issues.append(
                    build_issue(
                        f"generated-fallback-{inv.get('id')}",
                        "warning",
                        f"{label}: brak oryginalnego załącznika, dostępny tylko render.",
                    )
                )
            documents.append(
                build_document(
                    f"cost-{inv.get('id')}",
                    "costs",
                    label,
                    status,
                    source=source,
                    file_key=file_key,
                    message=message,
                )
            )

        names = [Path(item.key).name.casefold() for item in month_files]
        for vendor in EXPECTED_VENDORS:
            if vendor.skip or vendor.download_glob or vendor.name in matched_vendors:
                continue
            stored = any(
                vendor.pattern.casefold() in name or vendor.name.casefold() in name
                for name in names
            )
            documents.append(
                build_document(
                    f"vendor-{vendor.name.casefold().replace(' ', '-')}",
                    "costs",
                    vendor.name,
                    "present" if stored else "available_automatically",
                    source="Pobrany dokument" if stored else "Zoho Mail",
                    message=(
                        "Dokument jest już w katalogu kosztów."
                        if stored
                        else "Brak w Fakturowni; kolektor sprawdzi Zoho Mail."
                    ),
                )
            )
        return documents
