"""
zdrovena.api.models – Pydantic request/response models
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, field_validator, model_validator


class CloseRequest(BaseModel):
    year: int
    month: int
    dry_run: bool = False
    ignore_warnings: bool = False
    ignore_vendors: list[str] = []

    @model_validator(mode="after")
    def validate_date(self) -> "CloseRequest":
        if not (1 <= self.month <= 12):
            raise ValueError(f"Invalid month: {self.month}")
        if self.year < 2020:
            raise ValueError(f"Suspicious year: {self.year}")
        return self


class CloseResponse(BaseModel):
    sales_invoice_count: int
    sales_gross_total: str          # Decimal serialised as string — safe across JSON
    sales_pdfs_downloaded: int
    cost_invoice_count: int
    cost_found_vendors: dict[str, str]
    cost_missing_vendors: list[str]
    ksef_count: int
    bank_statement_found: bool
    zip_path: str | None            # Path → str
    email_sent: bool
    warnings: list[str]
    errors: list[str]
    steps_completed: list[str]
    has_critical_errors: bool

    @classmethod
    def from_close_report(cls, report: Any) -> "CloseResponse":
        """Convert a CloseReport dataclass to CloseResponse."""
        return cls(
            sales_invoice_count=report.sales_invoice_count,
            sales_gross_total=str(report.sales_gross_total),
            sales_pdfs_downloaded=report.sales_pdfs_downloaded,
            cost_invoice_count=report.cost_invoice_count,
            cost_found_vendors=report.cost_found_vendors,
            cost_missing_vendors=report.cost_missing_vendors,
            ksef_count=report.ksef_count,
            bank_statement_found=report.bank_statement_found,
            zip_path=str(report.zip_path) if report.zip_path else None,
            email_sent=report.email_sent,
            warnings=report.warnings,
            errors=report.errors,
            steps_completed=report.steps_completed,
            has_critical_errors=report.has_critical_errors,
        )


class ErrorResponse(BaseModel):
    detail: str
    code: str | None = None
