"""
zdrovena.api.models – Pydantic request/response models
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator


class CloseRequest(BaseModel):
    year: int
    month: int
    dry_run: bool = False
    ignore_warnings: bool = False
    ignore_vendors: list[str] = []

    @model_validator(mode="after")
    def validate_date(self) -> CloseRequest:
        if not (1 <= self.month <= 12):
            raise ValueError(f"Invalid month: {self.month}")
        if self.year < 2020:
            raise ValueError(f"Suspicious year: {self.year}")
        return self


class CloseResponse(BaseModel):
    sales_invoice_count: int
    sales_gross_total: str  # Decimal serialised as string — safe across JSON
    sales_pdfs_downloaded: int
    cost_invoice_count: int
    cost_found_vendors: dict[str, str]
    cost_missing_vendors: list[str]
    ksef_count: int
    bank_statement_found: bool
    zip_path: str | None  # Path → str
    zip_files: list[str] | None = None
    email_sent: bool
    warnings: list[str]
    errors: list[str]
    steps_completed: list[str]
    has_critical_errors: bool
    log_lines: list[str] = []

    @classmethod
    def from_close_report(cls, report: Any, *, log_lines: list[str] | None = None) -> CloseResponse:
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
            zip_files=report.zip_files,
            email_sent=report.email_sent,
            warnings=report.warnings,
            errors=report.errors,
            steps_completed=report.steps_completed,
            has_critical_errors=report.has_critical_errors,
            log_lines=log_lines or [],
        )


class ErrorResponse(BaseModel):
    detail: str
    code: str | None = None


class CloseStateResponse(BaseModel):
    completed_steps: list[str]


class CloseWorkflowActionRequest(BaseModel):
    year: int
    month: int
    confirm: bool = False
    override_reason: str | None = None
    ignore_vendors: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_date(self) -> CloseWorkflowActionRequest:
        if not (1 <= self.month <= 12):
            raise ValueError(f"Invalid month: {self.month}")
        if self.year < 2020:
            raise ValueError(f"Suspicious year: {self.year}")
        return self


class CloseWorkflowStep(BaseModel):
    status: str
    started_at: str | None = None
    completed_at: str | None = None
    message: str | None = None


class CloseWorkflowDocument(BaseModel):
    id: str
    category: str
    label: str
    status: str
    required: bool = True
    source: str | None = None
    file_key: str | None = None
    message: str | None = None


class CloseWorkflowIssue(BaseModel):
    id: str
    severity: str
    message: str
    stage: str


class CloseWorkflowArtifact(BaseModel):
    kind: str
    key: str
    files: list[str] = Field(default_factory=list)


class CloseWorkflowRunResponse(BaseModel):
    run_id: str
    year: int
    month: int
    status: str
    active_action: str | None = None
    requested_by: str
    created_at: str
    updated_at: str
    steps: dict[str, CloseWorkflowStep]
    documents: list[CloseWorkflowDocument] = Field(default_factory=list)
    issues: list[CloseWorkflowIssue] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[CloseWorkflowArtifact] = Field(default_factory=list)
    logs: list[str] = Field(default_factory=list)
    overrides: list[dict[str, Any]] = Field(default_factory=list)


class InvoiceItem(BaseModel):
    id: int
    number: str
    kind: str
    sell_date: str | None
    issue_date: str | None
    buyer_name: str | None
    price_net: str | None
    price_tax: str | None
    price_gross: str | None
    currency: str | None
    status: str | None

    @classmethod
    def from_fakturownia(cls, inv: dict[str, Any]) -> InvoiceItem:
        return cls(
            id=inv["id"],
            number=inv.get("number", ""),
            kind=inv.get("kind", "vat"),
            sell_date=inv.get("sell_date") or inv.get("issue_date"),
            issue_date=inv.get("issue_date"),
            buyer_name=inv.get("buyer_name"),
            price_net=str(inv["price_net"]) if inv.get("price_net") is not None else None,
            price_tax=str(inv["price_tax"]) if inv.get("price_tax") is not None else None,
            price_gross=str(inv["price_gross"]) if inv.get("price_gross") is not None else None,
            currency=inv.get("currency", "PLN"),
            status=inv.get("status"),
        )


class ProductItem(BaseModel):
    id: int
    name: str
    code: str | None
    price_net: str | None
    price_gross: str | None
    currency: str | None
    active: bool

    @classmethod
    def from_fakturownia(cls, p: dict[str, Any]) -> ProductItem:
        return cls(
            id=p["id"],
            name=p.get("name", ""),
            code=p.get("code") or p.get("sku"),
            price_net=str(p["price_net"]) if p.get("price_net") is not None else None,
            price_gross=str(p["price_gross"]) if p.get("price_gross") is not None else None,
            currency=p.get("currency", "PLN"),
            active=not p.get("disabled", False),
        )
