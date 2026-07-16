"""Operator-driven month-close workflow and read-only preflight dashboard."""

from __future__ import annotations

import calendar
import fnmatch
import logging
from io import StringIO
from pathlib import Path
from typing import Any

from zdrovena.audit.sections import check_numbering
from zdrovena.common import FakturowniaClient
from zdrovena.common.storage import StorageService, get_storage_service
from zdrovena.month_closing.close_history import append_close_history, build_history_entry
from zdrovena.month_closing.config import (
    BASE_DIR,
    EXPECTED_VENDORS,
    FAKTUROWNIA_REPORTS,
    POLISH_MONTHS,
    VendorConfig,
)
from zdrovena.month_closing.console import ConsoleReporter
from zdrovena.month_closing.orchestrator import CloseReport, MonthCloseOrchestrator
from zdrovena.month_closing.preflight import pko_matches_month
from zdrovena.month_closing.run_store import CloseRunStore

logger = logging.getLogger("zdrovena.month_closing.workflow")

COLLECTION_ACTIONS = ("sales", "costs", "reports", "bank")


def _document(
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


def _issue(
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
                _document(
                    "fakturownia",
                    "provider",
                    "Fakturownia",
                    "invalid",
                    source="Fakturownia API",
                    message="Nie udało się sprawdzić faktur.",
                )
            )
            issues.append(
                _issue(
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
            _document(
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
                _issue("sales-missing", "blocker", "Brak faktur sprzedażowych za wybrany okres.")
            )
        for series in check_numbering(sales):
            if series.gaps:
                issues.append(
                    _issue(
                        f"sales-gaps-{series.series}",
                        "blocker",
                        f"Braki w numeracji /{series.series}: {series.gaps}",
                    )
                )
            if series.duplicates:
                issues.append(
                    _issue(
                        f"sales-duplicates-{series.series}",
                        "blocker",
                        f"Duplikaty numeracji /{series.series}: {series.duplicates}",
                    )
                )

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
                _document(
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
                    _issue(
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
            _document(
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
                _issue(
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
                _document(
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
                    _issue(
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
                    _issue(
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
                    _issue(
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
                    _issue(
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
                    _issue(
                        f"generated-fallback-{inv.get('id')}",
                        "warning",
                        f"{label}: brak oryginalnego załącznika, dostępny tylko render.",
                    )
                )
            documents.append(
                _document(
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
                _document(
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


class MonthCloseWorkflow:
    """Coordinates claims, inspection and isolated execution stages."""

    def __init__(
        self,
        *,
        store: CloseRunStore | None = None,
        storage: StorageService | None = None,
    ) -> None:
        self.store = store or CloseRunStore.from_environment()
        self.storage = storage or get_storage_service()

    def get_run(self, year: int, month: int, requested_by: str) -> dict[str, Any]:
        return self.store.get_or_create(year, month, requested_by)

    def reset(self, year: int, month: int, requested_by: str) -> dict[str, Any]:
        return self.store.reset(year, month, requested_by)

    def perform(
        self,
        year: int,
        month: int,
        action: str,
        requested_by: str,
        *,
        confirm: bool = False,
        override_reason: str | None = None,
        ignore_vendors: list[str] | None = None,
    ) -> dict[str, Any]:
        run = self.store.try_claim(year, month, action, requested_by)
        try:
            if action == "check":
                inspected = MonthCloseInspector(
                    year,
                    month,
                    storage=self.storage,
                ).inspect()
                run.update(inspected)
                ready = bool(inspected["metrics"].get("ready"))
                return self.store.finish_action(
                    run,
                    action,
                    success=True,
                    message="Kontrola zakończona.",
                    status="ready" if ready else "needs_input",
                )

            should_refresh_inputs = action == "package" or (
                action == "send" and confirm and run["steps"]["package"]["status"] == "done"
            )
            if should_refresh_inputs:
                inspected = MonthCloseInspector(
                    year,
                    month,
                    storage=self.storage,
                ).inspect()
                run["documents"] = inspected["documents"]
                run["issues"] = inspected["issues"]
                run["metrics"].update(inspected["metrics"])
                self._apply_known_email_sources(run)

            prerequisite_error = self._validate_action(
                run,
                action,
                confirm,
                override_reason,
            )
            if prerequisite_error:
                run["issues"] = [issue for issue in run["issues"] if issue.get("stage") != action]
                run["issues"].append(
                    _issue(f"{action}-blocked", "blocker", prerequisite_error, stage=action)
                )
                return self.store.finish_action(
                    run,
                    action,
                    success=False,
                    message=prerequisite_error,
                    status="needs_input",
                )

            buffer = StringIO()
            orchestrator = MonthCloseOrchestrator(
                year=year,
                month=month,
                dry_run=False,
                non_interactive=True,
                ignore_vendors=ignore_vendors,
                manage_state=False,
                inbox_prefix=f"faktury/inbox/{year:04d}-{month:02d}",
            )
            orchestrator.out = ConsoleReporter(stream=buffer)
            report = orchestrator.execute_stage(action)
            self._apply_report(run, action, report, buffer.getvalue().splitlines())

            success, message = self._stage_result(action, report)
            if not success:
                return self.store.finish_action(
                    run,
                    action,
                    success=False,
                    message=message,
                    status="needs_input",
                )

            if action in COLLECTION_ACTIONS:
                action_issues = [issue for issue in run["issues"] if issue.get("stage") == action]
                inspected = MonthCloseInspector(
                    year,
                    month,
                    storage=self.storage,
                ).inspect()
                run["documents"] = inspected["documents"]
                run["issues"] = inspected["issues"] + action_issues
                run["metrics"].update(inspected["metrics"])
                if action == "costs":
                    self._apply_known_email_sources(run)

            if override_reason:
                run["overrides"].append(
                    {
                        "action": action,
                        "reason": override_reason,
                        "user": requested_by,
                    }
                )
            status = self._next_status(run, action)
            if action == "send":
                append_close_history(
                    self.storage,
                    build_history_entry(
                        year=year,
                        month=month,
                        month_name=POLISH_MONTHS[month],
                        status="success",
                        dry_run=False,
                        report=report,
                    ),
                )
            return self.store.finish_action(
                run,
                action,
                success=True,
                message=message,
                status=status,
            )
        except Exception as exc:
            logger.exception("Month-close action %s failed", action)
            run["issues"] = [issue for issue in run["issues"] if issue.get("stage") != action]
            run["issues"].append(_issue(f"{action}-error", "error", str(exc), stage=action))
            return self.store.finish_action(
                run,
                action,
                success=False,
                message=str(exc),
                status="failed",
            )

    @staticmethod
    def _validate_action(
        run: dict[str, Any],
        action: str,
        confirm: bool,
        override_reason: str | None,
    ) -> str | None:
        if action in COLLECTION_ACTIONS and run["steps"]["check"]["status"] != "done":
            return "Najpierw uruchom kontrolę wstępną."
        if action == "package":
            missing = [
                step for step in COLLECTION_ACTIONS if run["steps"][step]["status"] != "done"
            ]
            if missing:
                return f"Najpierw zakończ etapy: {', '.join(missing)}."
            blockers = [
                issue["message"]
                for issue in run["issues"]
                if issue.get("severity") in {"blocker", "error"}
            ]
            if blockers:
                return "Usuń blokujące problemy przed zbudowaniem paczki."
        if action == "send":
            if run["steps"]["package"]["status"] != "done":
                return "Najpierw zbuduj i przejrzyj paczkę."
            if any(issue.get("severity") in {"blocker", "error"} for issue in run["issues"]):
                return "Usuń blokujące problemy przed wysyłką."
            if not confirm:
                return "Wysyłka wymaga jawnego potwierdzenia operatora."
            if any(issue.get("severity") == "warning" for issue in run["issues"]) and not (
                override_reason and override_reason.strip()
            ):
                return "Wysyłka z ostrzeżeniami wymaga podania powodu."
        return None

    @staticmethod
    def _stage_result(action: str, report: CloseReport) -> tuple[bool, str]:
        if report.errors:
            return False, report.errors[-1]
        if action == "sales":
            return report.sales_invoice_count > 0, (
                f"Pobrano {report.sales_pdfs_downloaded} faktur sprzedażowych."
            )
        if action == "costs":
            if report.cost_missing_vendors:
                return False, f"Brakuje: {', '.join(report.cost_missing_vendors)}."
            return True, f"Zebrano {report.cost_invoice_count} dokumentów kosztowych."
        if action == "reports":
            complete = any("JPK & VAT" in step for step in report.steps_completed)
            return complete, (
                "Raporty są kompletne." if complete else "Nadal brakuje raportów JPK/VAT."
            )
        if action == "bank":
            return report.bank_statement_found, (
                "Wyciąg bankowy jest gotowy."
                if report.bank_statement_found
                else "Nadal brakuje wyciągu bankowego."
            )
        if action == "package":
            return report.zip_path is not None, "Paczka ZIP została zbudowana."
        if action == "send":
            return report.email_sent, "Paczka została wysłana."
        return False, f"Nieznany etap: {action}"

    @staticmethod
    def _next_status(run: dict[str, Any], action: str) -> str:
        if action == "package":
            return "package_ready"
        if action == "send":
            return "completed"
        if all(
            step == action or run["steps"][step]["status"] == "done" for step in COLLECTION_ACTIONS
        ):
            return "awaiting_review"
        return "collecting"

    @staticmethod
    def _apply_report(
        run: dict[str, Any],
        action: str,
        report: CloseReport,
        log_lines: list[str],
    ) -> None:
        safe_log_lines = [line[:500] for line in log_lines]
        run["logs"] = (run.get("logs", []) + safe_log_lines)[-80:]
        run["issues"] = [issue for issue in run.get("issues", []) if issue.get("stage") != action]
        for idx, warning in enumerate(report.warnings):
            severity = (
                "blocker"
                if (
                    "Brak faktur kosztowych" in warning
                    or "reports incomplete" in warning
                    or "Wyciąg PKO" in warning
                )
                else "warning"
            )
            run["issues"].append(_issue(f"{action}-warning-{idx}", severity, warning, stage=action))
        run["metrics"].update(
            {
                "sales_invoice_count": report.sales_invoice_count,
                "sales_gross_total": str(report.sales_gross_total),
                "sales_pdfs_downloaded": report.sales_pdfs_downloaded,
                "cost_invoice_count": report.cost_invoice_count,
                "cost_found_vendors": report.cost_found_vendors,
                "cost_missing_vendors": report.cost_missing_vendors,
                "bank_statement_found": report.bank_statement_found,
            }
        )
        if report.zip_path:
            run["artifacts"] = [
                artifact for artifact in run["artifacts"] if artifact.get("kind") != "package"
            ]
            run["artifacts"].append(
                {
                    "kind": "package",
                    "key": str(report.zip_path),
                    "files": report.zip_files or [],
                }
            )

    @staticmethod
    def _apply_known_email_sources(run: dict[str, Any]) -> None:
        found_vendors = run.get("metrics", {}).get("cost_found_vendors", {})
        if not isinstance(found_vendors, dict):
            return
        for vendor, source in found_vendors.items():
            if source != "Zoho Mail":
                continue
            vendor_name = str(vendor)
            run["issues"] = [
                issue
                for issue in run["issues"]
                if vendor_name.casefold() not in issue["message"].casefold()
            ]
            for document in run["documents"]:
                if document["category"] == "costs" and document["label"].casefold().startswith(
                    vendor_name.casefold()
                ):
                    document["status"] = "present"
                    document["source"] = "Oryginalny załącznik Zoho Mail"
                    document["message"] = "Oryginalny PDF został pobrany z wiadomości e-mail."
