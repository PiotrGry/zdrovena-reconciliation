"""Month close end to end: the real workflow and orchestrator, fakes only at the edges.

Written before any fix (2026-11-21), to show how month close behaves today. The operator saw files
left behind in the inbox after a close, which the #480 hotfix worked around without fixing.

Real: MonthCloseWorkflow, MonthCloseOrchestrator, the preflight checker, the ZIP builder,
LocalStorageService (blob storage on disk), the run store.
Fake: the Fakturownia API, the e-mail sender, Zoho Mail and KSeF (switched off).

The stages are the ones production runs (/api/close/workflow/actions/{action}):
check → sales → costs → reports → bank → package → send.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

import pytest

from zdrovena.common.storage import LocalStorageService
from zdrovena.month_closing.run_store import CloseRunStore
from zdrovena.month_closing.workflow import MonthCloseWorkflow

YEAR, MONTH = 2026, 10
INBOX = f"faktury/inbox/{YEAR:04d}-{MONTH:02d}"
OTHER_INBOX = f"faktury/inbox/{YEAR:04d}-{MONTH - 1:02d}"
# Vendors whose invoices come from Fakturownia or Zoho; the fake has none, so the operator
# waives them, as in production when a vendor did not invoice this month.
NO_INVOICE_VENDORS = [
    "Shopify",
    "Allegro",
    "PayU",
    "InPost",
    "Apaczka",
    "PulsePure",
    "Accounting/Bożena",
]


def text_pdf(text: str) -> bytes:
    """A one-page PDF whose text pypdf can extract (the bank statement is detected by content)."""
    stream = f"BT /F1 12 Tf 50 750 Td ({text}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    out += (
        f"trailer << /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(out)


# What the operator uploads for October 2026, as file name → content.
INBOX_FILES: dict[str, bytes] = {
    "invoice-12345-67890123.pdf": text_pdf("Canva invoice"),  # Canva (download_glob)
    "1234567890.pdf": text_pdf("Google Ads invoice"),  # Google Ads (download_glob)
    "wyciag_pazdziernik.pdf": text_pdf("PKO BP Wyciag z rachunku pazdziernik 2026"),
    "zdrovena-2026-10-jpk_fa.xml": b"<?xml version='1.0'?><JPK><JPK_FA/></JPK>",
    "zdrovena-2026-10-v7m.xml": b"<?xml version='1.0'?><JPK><JPK_V7M/></JPK>",
    "zdrovena-2026-10-31_rejestr.pdf": text_pdf("Wykaz sprzedazy VAT"),
    # Not any expected document: the operator adds such files too (a contract, a note).
    "umowa-najmu.pdf": text_pdf("Umowa najmu lokalu"),
}


@dataclass
class FakeFakturownia:
    """The Fakturownia API: two sales invoices, no cost invoices."""

    sales: list[dict] = field(
        default_factory=lambda: [
            {
                "id": 1,
                "number": "FV 1/10/2026",
                "sell_date": "2026-10-05",
                "issue_date": "2026-10-05",
            },
            {
                "id": 2,
                "number": "FV 2/10/2026",
                "sell_date": "2026-10-20",
                "issue_date": "2026-10-20",
            },
        ]
    )

    def fetch_sales_invoices(self, date_from, date_to):
        return list(self.sales)

    def download_all_pdfs(self, invoices, target_dir: Path, dry_run: bool = False):
        target_dir.mkdir(parents=True, exist_ok=True)
        saved = []
        for invoice in invoices:
            path = target_dir / f"{invoice['number'].replace('/', '_').replace(' ', '_')}.pdf"
            if not dry_run:
                path.write_bytes(text_pdf(invoice["number"]))
            saved.append(path)
        return saved

    def fetch_cost_invoices(self, date_from, date_to):
        return []

    def cost_document_stem(self, invoice):
        return f"koszt_{invoice['id']}"

    def download_cost_documents(self, *args, **kwargs):
        return []


@dataclass
class SentMail:
    to: str
    subject: str
    zip_names: list[str]
    contents: set[bytes] = field(default_factory=set)


class FakeEmail:
    sent: ClassVar[list[SentMail]] = []

    def __init__(self, smtp_password: str = "") -> None:
        pass

    def send_report(self, *, to_email, subject, body, attachments):
        names: list[str] = []
        contents: set[bytes] = set()
        for attachment in attachments:
            with zipfile.ZipFile(io.BytesIO(Path(attachment).read_bytes())) as archive:
                names.extend(archive.namelist())
                contents.update(archive.read(name) for name in archive.namelist())
        FakeEmail.sent.append(SentMail(to_email, subject, names, contents))


@pytest.fixture
def close_env(tmp_path, monkeypatch):
    storage = LocalStorageService(root=tmp_path / "blob")
    for name, content in INBOX_FILES.items():
        source = tmp_path / "upload" / name
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(content)
        storage.upload(source, f"{INBOX}/{name}")
    older = tmp_path / "upload" / "wrzesien.pdf"
    older.write_bytes(text_pdf("September document"))
    storage.upload(older, f"{OTHER_INBOX}/wrzesien.pdf")

    monkeypatch.setenv("PROVIDER_MODE", "fake")  # KSeF off
    FakeEmail.sent = []
    fake_fakturownia = FakeFakturownia()
    local_base = tmp_path / "local"
    patches = [
        patch("zdrovena.month_closing.orchestrator.get_storage_service", return_value=storage),
        patch("zdrovena.month_closing.orchestrator.BASE_DIR", local_base),
        patch(
            "zdrovena.month_closing.orchestrator.FakturowniaClient.from_keyring",
            return_value=fake_fakturownia,
        ),
        patch("zdrovena.month_closing.orchestrator.EmailService", FakeEmail),
        patch(
            "zdrovena.month_closing.orchestrator.MonthCloseOrchestrator._get_secret",
            return_value=None,
        ),
        patch(
            "zdrovena.month_closing.preflight.DOWNLOAD_WATCH_DIR", tmp_path / "no-local-downloads"
        ),
        # The warehouse audit (WZ documents) is its own subject, outside the document flow.
        patch("zdrovena.month_closing.inspection.warehouse_issues", return_value=[]),
    ]
    for item in patches:
        item.start()
    workflow = MonthCloseWorkflow(
        store=CloseRunStore(local_root=tmp_path / "runs"), storage=storage
    )
    yield workflow, storage
    for item in patches:
        item.stop()


def run_all_stages(workflow: MonthCloseWorkflow) -> dict:
    run = {}
    for action in ("check", "sales", "costs", "reports", "bank", "package", "send"):
        run = workflow.perform(
            YEAR,
            MONTH,
            action,
            "test@humio",
            confirm=(action == "send"),
            ignore_vendors=NO_INVOICE_VENDORS if action == "costs" else None,
        )
    return run


def stored_contents(storage: LocalStorageService, prefix: str) -> set[bytes]:
    """Contents of every stored file under prefix, including files inside ZIP packages.

    Reports are renamed by design (JPK_FA.xml …), so a document is identified by its content.
    """
    contents: set[bytes] = set()
    for key in keys(storage, prefix):
        data = b"".join(storage.stream(key))
        contents.add(data)
        if key.endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                contents.update(archive.read(name) for name in archive.namelist())
    return contents


def keys(storage: LocalStorageService, prefix: str) -> list[str]:
    return sorted(item.key for item in storage.list_files(prefix.rstrip("/") + "/"))


# ── The whole close ─────────────────────────────────────────────────────────


def test_every_stage_completes_and_the_package_is_sent_once(close_env):
    workflow, _storage = close_env
    run = run_all_stages(workflow)
    statuses = {step: run["steps"][step]["status"] for step in run["steps"]}
    not_done = {
        step: {k: run["steps"][step].get(k) for k in ("status", "message", "detail")}
        for step, status in statuses.items()
        if status != "done"
    }
    assert not not_done, f"stages not done: {not_done}; issues: {run.get('issues')}"
    assert len(FakeEmail.sent) == 1
    # The subject uses the month name without Polish letters ("Pazdziernik").
    assert "pazdziernik 2026" in FakeEmail.sent[0].subject.casefold()


def test_the_package_holds_every_uploaded_document(close_env):
    workflow, _storage = close_env
    run_all_stages(workflow)
    names = FakeEmail.sent[0].zip_names
    joined = "\n".join(names)
    # The sales invoices from Fakturownia.
    assert "FV_1_10_2026.pdf" in joined and "FV_2_10_2026.pdf" in joined
    # Every inbox document, recognised or not, is in the package (reports under their new names).
    for name, content in INBOX_FILES.items():
        assert content in FakeEmail.sent[0].contents, f"{name} is missing from the package: {names}"


@pytest.mark.xfail(
    strict=True,
    reason="BUG: documents the preflight does not recognise (and, since #480, every file of the "
    "scoped inbox) are copied into the month package but never removed from the inbox.",
)
def test_after_the_close_the_month_inbox_is_empty(close_env):
    """The operator's bug: documents stay in the inbox after the month is closed and sent."""
    workflow, storage = close_env
    run_all_stages(workflow)
    assert keys(storage, INBOX) == []


def test_the_close_never_touches_another_months_inbox(close_env):
    workflow, storage = close_env
    run_all_stages(workflow)
    assert keys(storage, OTHER_INBOX) == [f"{OTHER_INBOX}/wrzesien.pdf"]


def test_every_inbox_document_is_kept_in_the_month_storage(close_env):
    """Leaving the inbox must mean being stored with the month, never being dropped."""
    workflow, storage = close_env
    run_all_stages(workflow)
    stored = stored_contents(storage, f"faktury/{YEAR}")
    for name, content in INBOX_FILES.items():
        assert content in stored, f"{name} is not stored with the month"


# ── Safety ──────────────────────────────────────────────────────────────────


@pytest.mark.xfail(
    strict=True,
    reason="BUG: PreflightChecker.copy_to_folders deletes the inbox blob right after the local "
    "copy, before the month storage upload; when that upload fails the document is lost.",
)
def test_a_document_never_leaves_the_inbox_before_it_is_stored_with_the_month(close_env):
    """If copying to the month storage fails, the inbox copy must stay: it is the only one."""
    workflow, storage = close_env
    original_upload = storage.upload

    def failing_upload(source, key, *args, **kwargs):
        if key.startswith(f"faktury/{YEAR}/") and "inbox" not in key:
            raise OSError("storage unavailable")
        return original_upload(source, key, *args, **kwargs)

    with patch.object(storage, "upload", side_effect=failing_upload):
        for action in ("check", "costs", "reports", "bank"):
            try:
                workflow.perform(
                    YEAR,
                    MONTH,
                    action,
                    "test@humio",
                    ignore_vendors=NO_INVOICE_VENDORS if action == "costs" else None,
                )
            except Exception:
                pass
    kept = stored_contents(storage, "faktury")
    lost = [name for name, content in INBOX_FILES.items() if content not in kept]
    assert lost == [], f"lost for good: {lost}"


def test_a_failed_send_keeps_the_inbox_for_a_retry(close_env):
    workflow, storage = close_env

    class BrokenEmail(FakeEmail):
        def send_report(self, **kwargs):
            raise OSError("SMTP down")

    with patch("zdrovena.month_closing.orchestrator.EmailService", BrokenEmail):
        try:
            run_all_stages(workflow)
        except Exception:
            pass
    kept = stored_contents(storage, "faktury")
    for name, content in INBOX_FILES.items():
        assert content in kept, f"{name} lost after a failed send"


def test_a_late_upload_after_packaging_goes_into_the_next_package(close_env, tmp_path):
    """A document uploaded after "package" must reach the accountant on the re-package."""
    workflow, storage = close_env
    for action in ("check", "sales", "costs", "reports", "bank", "package"):
        workflow.perform(
            YEAR,
            MONTH,
            action,
            "test@humio",
            ignore_vendors=NO_INVOICE_VENDORS if action == "costs" else None,
        )
    late = tmp_path / "late.pdf"
    late.write_bytes(text_pdf("Faktura dostarczona pozniej"))
    storage.upload(late, f"{INBOX}/spozniona-faktura.pdf")
    workflow.perform(YEAR, MONTH, "package", "test@humio")
    workflow.perform(YEAR, MONTH, "send", "test@humio", confirm=True)
    assert any("spozniona-faktura.pdf" in name for name in FakeEmail.sent[-1].zip_names)
