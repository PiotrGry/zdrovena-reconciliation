"""
zdrovena.month_closing.orchestrator – Month Close Orchestrator
================================================================
Central controller that runs the full monthly accounting close pipeline:

  0. Pre-flight: check inbox/ for manual invoices, reports & bank statement
  1. Create folder structure
  2. Download sales invoices (Fakturownia)
  3. Verify JPK_FA, JPK_V7M, VAT register
  4. Download cost invoices (KSeF verify → Fakturownia PDFs → Zoho Mail)
  5. Check for bank statement
  ── Warnings gate: abort if ANY issues detected ──
  6. Create ZIP archive
  7. Email package to accountant
  8. Print final summary report
"""

from __future__ import annotations

import calendar
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from zdrovena.audit.sections import check_numbering
from zdrovena.common import FakturowniaClient
from zdrovena.common.formatting import to_decimal
from zdrovena.common.secrets import get_secret as _get_secret_impl
from zdrovena.common.storage import get_storage_service
from zdrovena.month_closing.canva_downloader import download_canva_invoice
from zdrovena.month_closing.config import (
    ACCOUNTANT_EMAIL,
    BASE_DIR,
    COMPANY_BRAND,
    COST_INVOICE_OVERLAP_DAYS,
    ENGLISH_MONTHS,
    EXPECTED_VENDORS,
    FAKTUROWNIA_REPORTS,
    KEYCHAIN_SERVICE_ZOHO_CLIENT_ID,
    KEYCHAIN_SERVICE_ZOHO_CLIENT_SECRET,
    KEYCHAIN_SERVICE_ZOHO_REFRESH_TOKEN,
    KEYCHAIN_SERVICE_ZOHO_SMTP,
    KSEF_ENABLED,
    POLISH_MONTHS,
    VendorConfig,
)
from zdrovena.month_closing.console import ConsoleReporter
from zdrovena.month_closing.email_service import EmailService
from zdrovena.month_closing.fakturownia_reports import download_fakturownia_reports
from zdrovena.month_closing.invoice_date_check import (
    delete_rejected,
    move_unverified,
    validate_invoice_dates,
)
from zdrovena.month_closing.ksef import KSeFClient
from zdrovena.month_closing.preflight import PreflightChecker
from zdrovena.month_closing.state import PipelineState
from zdrovena.month_closing.zip_service import create_month_archive, create_month_archive_from_blob
from zdrovena.month_closing.zoho_mail import ZohoMailClient

logger = logging.getLogger("zdrovena.month_closing.orchestrator")


@dataclass
class CloseReport:
    sales_invoice_count: int = 0
    sales_gross_total: Decimal = Decimal("0.00")
    sales_pdfs_downloaded: int = 0
    cost_invoice_count: int = 0
    cost_found_vendors: dict[str, str] = field(default_factory=dict)
    cost_missing_vendors: list[str] = field(default_factory=list)
    ksef_count: int = 0
    bank_statement_found: bool = False
    zip_path: Path | None = None
    email_sent: bool = False
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    steps_completed: list[str] = field(default_factory=list)

    @property
    def has_critical_errors(self) -> bool:
        return len(self.errors) > 0


class MonthCloseOrchestrator:
    def __init__(
        self,
        year: int,
        month: int,
        dry_run: bool = False,
        *,
        non_interactive: bool = False,
        ignore_warnings: bool = False,
        ignore_vendors: list[str] | None = None,
    ) -> None:
        if not (1 <= month <= 12):
            raise ValueError(f"Invalid month: {month}")
        if year < 2020:
            raise ValueError(f"Suspicious year: {year}")

        self.year = year
        self.month = month
        self.dry_run = dry_run
        self.non_interactive = non_interactive
        self.ignore_warnings = ignore_warnings
        self.ignore_vendors: set[str] = {v.lower() for v in (ignore_vendors or [])}
        self.month_pl = POLISH_MONTHS[month]
        self.month_en = ENGLISH_MONTHS[month]
        last_day = calendar.monthrange(year, month)[1]
        self.date_from = f"{year}-{month:02d}-01"
        self.date_to = f"{year}-{month:02d}-{last_day:02d}"
        self.date_from_obj = date(year, month, 1)
        self.date_to_obj = date(year, month, last_day)

        last_date = self.date_to_obj
        cost_end = last_date + timedelta(days=COST_INVOICE_OVERLAP_DAYS)
        self.cost_date_to = cost_end.isoformat()

        self.month_dir = BASE_DIR / str(year) / self.month_pl
        self.sales_dir = self.month_dir / "sprzedaz"
        self.costs_dir = self.month_dir / "koszty"

        self.storage = get_storage_service()
        self._blob_prefix = f"faktury/{year}/{self.month_pl}"
        self.report = CloseReport()
        self.state = PipelineState(
            self.month_dir,
            storage=self.storage,
            blob_key=f"{self._blob_prefix}/.state.json",
        )
        self.out = ConsoleReporter()

    @staticmethod
    def _get_secret(service: str, required: bool = True) -> str | None:
        return _get_secret_impl(service, required=required)

    def _skip_if_done(self, step_name: str) -> bool:
        if self.state.is_done(step_name):
            self.out.skip(f"{step_name} (already done — skipping)")
            # Do NOT add to report.steps_completed — checkpoint steps are tracked
            # separately via state.completed_steps. report.steps_completed contains
            # only steps completed IN THIS run so history counts are accurate.
            return True
        return False

    def _upload_to_blob(self, local_path: Path, blob_dir_key: str) -> None:
        """Upload a single local file to blob storage. No-op in dry_run."""
        if self.dry_run or not local_path.exists():
            return
        try:
            key = f"{blob_dir_key}/{local_path.name}"
            self.storage.upload(local_path, key)
            logger.debug("Uploaded to blob: %s", key)
        except Exception as exc:
            logger.warning("Blob upload failed for %s: %s", local_path.name, exc)

    def _upload_dir_to_blob(self, local_dir: Path, blob_dir_key: str) -> None:
        """Upload all files in a directory to blob storage. No-op in dry_run."""
        if self.dry_run or not local_dir.exists():
            return
        for f in local_dir.iterdir():
            if f.is_file():
                self._upload_to_blob(f, blob_dir_key)

    def _blob_file_exists(self, blob_dir_key: str, filename: str) -> bool:
        """Check if a file exists in blob storage under the given prefix."""
        try:
            blobs = self.storage.list_files(blob_dir_key + "/")
            return any(
                b.key.endswith(f"/{filename}") or b.key == f"{blob_dir_key}/{filename}"
                for b in blobs
            )
        except Exception:
            return False

    def _mark_step_done(self, step_name: str) -> None:
        self.report.steps_completed.append(step_name)
        if not self.dry_run:
            self.state.mark_done(step_name)

    # ── Execution modes ──────────────────────────────────────────────────────

    def execute(self) -> CloseReport:
        mode = "DRY-RUN" if self.dry_run else "LIVE"
        self.out.banner(f"HUMIO Monthly Close – {self.month_en} {self.year}  [{mode}]")
        try:
            self._step_0_preflight()
            self._step_1_create_folders()
            self._step_2_sales_invoices()
            self._step_3_jpk_reports()
            self._step_4_cost_invoices()
            self._step_5_bank_statement()
            self._check_warnings_gate()
            self._step_6_zip_archive()
            self._step_7_email()
        except Exception as exc:
            self.report.errors.append(str(exc))
            logger.critical("Pipeline aborted: %s", exc, exc_info=True)
            raise
        finally:
            self._print_summary()
        return self.report

    def execute_zip_only(self) -> CloseReport:
        self.out.banner(f"HUMIO – ZIP only – {self.month_en} {self.year}")
        try:
            self._step_0_preflight()
            self._step_1_create_folders()
            self._step_2_sales_invoices()
            self._step_3_jpk_reports()
            self._step_4_cost_invoices()
            self._step_5_bank_statement()
            self._check_warnings_gate()
            self._step_6_zip_archive()
        except Exception as exc:
            self.report.errors.append(str(exc))
            logger.critical("Pipeline aborted: %s", exc, exc_info=True)
            raise
        finally:
            self._print_summary()
        return self.report

    def execute_send_only(self) -> CloseReport:
        self.out.banner(f"HUMIO – Send email only – {self.month_en} {self.year}")
        zip_name = f"{self.month_pl}_{self.year}_HUMIO.zip"
        zip_path = self.month_dir / zip_name
        if zip_path.exists():
            self.report.zip_path = zip_path
            self.out.info(f"📦 Using existing ZIP: {zip_path.name}")
        else:
            self.report.errors.append(f"ZIP not found: {zip_path}. Run with --zip first.")
            self.out.error(f"ZIP not found: {zip_path}")
            self.out.detail(f"Run first: zdrovena close {self.year}-{self.month:02d} --zip")
            self._print_summary()
            return self.report
        self._step_7_email()
        self._print_summary()
        return self.report

    def execute_zip_and_send(self) -> CloseReport:
        self.out.banner(f"HUMIO – ZIP + Send – {self.month_en} {self.year}")
        try:
            self._step_6_zip_archive()
            self._step_7_email()
        except Exception as exc:
            self.report.errors.append(str(exc))
            logger.critical("ZIP+Send failed: %s", exc, exc_info=True)
            raise
        finally:
            self._print_summary()
        return self.report

    # ── Pipeline steps ───────────────────────────────────────────────────────

    def _step_0_preflight(self) -> None:
        self.out.step(0, "Pre-flight: manual invoices, reports & bank statement")
        checker = PreflightChecker(
            year=self.year,
            month=self.month,
            month_dir=self.month_dir,
            date_from=self.date_from,
            date_to=self.date_to,
            cost_date_to=self.cost_date_to,
            dry_run=self.dry_run,
            get_secret=self._get_secret,
            no_browser=self.non_interactive,
            storage=self.storage,
        )
        pf = checker.run()
        self.report.bank_statement_found = pf.bank_statement_found
        self.report.warnings.extend(pf.warnings)
        self._preflight_checker = checker

        blockers = checker.build_blockers()
        if blockers:
            self.out.blocker_box(blockers)
            self.out.plain()
            self.out.plain("  Place missing files in: inbox/")
            self.out.plain(
                f"  Then rerun:  zdrovena close {self.year}-{self.month:02d}"
                f"{' --dry-run' if self.dry_run else ''}"
            )
            self.out.plain()
            raise SystemExit(1)
        self._mark_step_done("Pre-flight")

    def _step_1_create_folders(self) -> None:
        self.out.step(1, "Creating folder structure")
        self.sales_dir.mkdir(parents=True, exist_ok=True)
        self.costs_dir.mkdir(parents=True, exist_ok=True)
        logger.info("Folders ready: %s", self.month_dir)
        checker = getattr(self, "_preflight_checker", None)
        if checker is not None:
            checker.copy_to_folders(self.month_dir, self.costs_dir)
            # Upload pre-flight files to blob for production persistence
            self._upload_dir_to_blob(self.month_dir, self._blob_prefix)
            self._upload_dir_to_blob(self.costs_dir, f"{self._blob_prefix}/koszty")
        self._mark_step_done("Folder structure")
        self.out.ok(f"{self.month_dir} → blob:{self._blob_prefix}")

    def _step_2_sales_invoices(self) -> None:
        self.out.step(2, "Downloading sales invoices (Fakturownia)")
        if self._skip_if_done("Sales invoices"):
            return
        client = FakturowniaClient.from_keyring()
        invoices = client.fetch_sales_invoices(self.date_from, self.date_to)
        self.report.sales_invoice_count = len(invoices)
        self.report.sales_gross_total = sum(
            (to_decimal(inv.get("price_gross", 0)) for inv in invoices),
            Decimal(0),
        )
        self._sales_invoices = invoices
        if not invoices:
            raise RuntimeError(
                f"No sales invoices found for {self.date_from} – {self.date_to}. "
                "Check Fakturownia for the correct date range."
            )
        self.out.ok(
            f"{len(invoices)} invoice(s), gross total: {self.report.sales_gross_total:,.2f} PLN"
        )

        # Numbering gap check — abort before downloading if gaps found
        numbering_ok = True
        for sr in check_numbering(invoices):
            if sr.gaps:
                numbering_ok = False
                msg = (
                    f"Numeracja /{sr.series}: jest {sr.count}, "
                    f"oczekiwano {sr.expected} — brakuje: {sr.gaps}"
                )
                self.out.warn(msg)
                self.report.warnings.append(msg)
            elif sr.duplicates:
                numbering_ok = False
                msg = f"Numeracja /{sr.series}: duplikaty: {sr.duplicates}"
                self.out.warn(msg)
                self.report.warnings.append(msg)
            else:
                self.out.ok(f"Numeracja /{sr.series}: {sr.first}–{sr.last} ({sr.count} dok.)")

        if not numbering_ok:
            raise RuntimeError(
                "Brakuje faktur — uzupełnij numerację w Fakturowni przed zamknięciem miesiąca."
            )

        saved = client.download_all_pdfs(invoices, self.sales_dir, dry_run=self.dry_run)
        self.report.sales_pdfs_downloaded = len(saved)
        blob_sales = f"{self._blob_prefix}/sprzedaz"
        for pdf_path in saved:
            self._upload_to_blob(pdf_path, blob_sales)
        self._mark_step_done("Sales invoices")

    def _step_3_jpk_reports(self) -> None:
        self.out.step(3, "Verifying JPK and VAT reports")

        missing = [
            rpt
            for rpt in FAKTUROWNIA_REPORTS
            if not (self.month_dir / rpt["dest_name"]).exists()
            and not self._blob_file_exists(self._blob_prefix, rpt["dest_name"])
        ]
        for rpt in FAKTUROWNIA_REPORTS:
            if rpt not in missing:
                self.out.ok(f"{rpt['name']}: {rpt['dest_name']}")

        if missing and not self.non_interactive and not self.dry_run:
            self.out.info(f"🌐 {len(missing)} report(s) missing — launching browser session...")
            try:
                downloaded = download_fakturownia_reports(
                    missing,
                    self.date_from,
                    self.date_to,
                    self.month_dir,
                )
                for rpt_cfg, path in downloaded:
                    self.out.ok(f"{rpt_cfg['name']}: downloaded → {path.name}")
                    missing = [r for r in missing if r["name"] != rpt_cfg["name"]]
                    self._upload_to_blob(path, self._blob_prefix)
            except Exception as exc:
                logger.warning("Playwright report download failed: %s", exc)
                self.out.warn(f"Auto-download failed: {exc}")

        for rpt in missing:
            self.out.warn(f"{rpt['name']}: MISSING — {self.month_dir / rpt['dest_name']}")

        if not missing:
            pass  # all found
        elif self.dry_run:
            self.out.warn(
                f"dry_run: {len(missing)} JPK report(s) not yet in month folder — OK for simulation"
            )
        else:
            msg = (
                f"JPK/VAT reports incomplete: {', '.join(r['name'] for r in missing)}. "
                "Download from Fakturownia UI and upload via Inbox."
            )
            self.report.warnings.append(msg)
            self.out.warn(msg)
        self._mark_step_done("JPK & VAT reports")

    def _step_4_cost_invoices(self) -> None:
        self.out.step(4, "Collecting cost invoices")
        if self._skip_if_done("Cost invoices"):
            return
        self.out.info(
            f"📅 Issue date: {self.date_from} → {self.cost_date_to} "
            f"(overlap window: +{COST_INVOICE_OVERLAP_DAYS} days for early-next-month invoices)"
        )
        found_vendors: dict[str, str] = {}
        total_cost_files = 0

        # Phase 1: KSeF
        ksef_numbers: set[str] = set()
        if KSEF_ENABLED:
            self.out.section_start("Phase 1: KSeF verification")
            ksef_client = KSeFClient()
            ksef_client.authenticate()
            ksef_invoices = ksef_client.query_purchase_invoices(self.date_from, self.cost_date_to)
            self.report.ksef_count = len(ksef_invoices)
            ksef_numbers = {
                inv.get("ksefNumber", "") for inv in ksef_invoices if inv.get("ksefNumber")
            }
            for kinv in ksef_invoices:
                seller = kinv.get("seller", {}).get("name", "?")
                knum = kinv.get("ksefNumber", "?")
                logger.info("  KSeF invoice: %s | %s", seller, knum)
            self.out.item(f"📋 KSeF: {len(ksef_invoices)} purchase invoice(s)")
        else:
            self.out.section_start("Phase 1: KSeF (disabled)")

        # Phase 2: Fakturownia
        self.out.section_mid("Phase 2: Fakturownia (cost invoices + PDFs)")
        fakt_client = FakturowniaClient.from_keyring()
        fakt_invoices = fakt_client.fetch_cost_invoices(self.date_from, self.cost_date_to)
        self._cost_invoices = fakt_invoices

        # Phase 2a: Cross-verify KSeF vs Fakturownia
        if KSEF_ENABLED and ksef_numbers:
            fakt_gov_ids = {inv.get("gov_id", "") for inv in fakt_invoices if inv.get("gov_id")}
            missing_in_fakt = ksef_numbers - fakt_gov_ids
            if missing_in_fakt:
                for knum in sorted(missing_in_fakt):
                    self.out.item(f"❌ KSeF invoice NOT in Fakturownia: {knum}")
                msg = (
                    f"{len(missing_in_fakt)} KSeF invoice(s) missing in Fakturownia: "
                    f"{', '.join(sorted(missing_in_fakt))}. "
                    "Fakturownia auto-fetches every 15 min — click 'Pobierz faktury z KSeF' to sync."
                )
                self.report.warnings.append(msg)
                self.out.warn(msg)
            self.out.item(
                f"✅ KSeF cross-check: all {len(ksef_numbers)} invoice(s) found in Fakturownia"
            )

        # Narrow fakt_invoices to the actual month (fetch was wide to catch early-next-month invoices)
        fakt_invoices = [
            inv
            for inv in fakt_invoices
            if self.date_from
            <= (inv.get("sell_date") or inv.get("issue_date") or "")
            <= self.date_to
        ]

        for vendor_cfg in EXPECTED_VENDORS:
            if vendor_cfg.skip:
                continue
            pat = vendor_cfg.pattern.lower()
            for inv in fakt_invoices:
                buyer = (inv.get("buyer_name") or "").lower()
                buyer_nip = (inv.get("buyer_tax_no") or "").lower()
                if pat in buyer or pat in buyer_nip:
                    source = "Fakturownia (KSeF)" if inv.get("gov_id") else "Fakturownia"
                    if vendor_cfg.name not in found_vendors:
                        found_vendors[vendor_cfg.name] = source
                    break

        if fakt_invoices:
            saved = fakt_client.download_cost_pdfs(
                fakt_invoices, self.costs_dir, dry_run=self.dry_run
            )
            total_cost_files += len(saved) if not self.dry_run else len(fakt_invoices)
            cost_gross = sum(to_decimal(inv.get("price_gross", 0)) for inv in fakt_invoices)
            self.out.item(
                f"✅ Fakturownia: {len(fakt_invoices)} expense(s), gross total: {cost_gross:,.2f} PLN"
            )
            for inv in fakt_invoices:
                vendor = inv.get("buyer_name", "?")
                number = inv.get("number", "?")
                gross = inv.get("price_gross", 0)
                gov = inv.get("gov_id") or ""
                tag = " [KSeF]" if gov else ""
                logger.info("  Cost: %s | %s | %s PLN%s", vendor, number, gross, tag)
        else:
            self.out.item("ℹ️  No cost invoices in Fakturownia for this period")

        # Phase 2b: Pre-flight manual vendors
        checker = getattr(self, "_preflight_checker", None)
        preflight_matches = checker.result.matches if checker else []
        for vendor_cfg, _src_pdf in preflight_matches:
            vname = vendor_cfg.name if isinstance(vendor_cfg, VendorConfig) else vendor_cfg["name"]
            if vname not in found_vendors:
                found_vendors[vname] = "Pre-flight (manual)"
                total_cost_files += 1

        # Phase 3: Zoho Mail
        still_missing = [
            v
            for v in EXPECTED_VENDORS
            if v.name not in found_vendors
            and not v.manual
            and not v.skip
            and not v.browser_download
            and v.name.lower() not in self.ignore_vendors
        ]
        browser_pending = [
            v
            for v in EXPECTED_VENDORS
            if v.browser_download
            and v.name not in found_vendors
            and v.invoice_id_re
            and not v.skip
            and v.name.lower() not in self.ignore_vendors
        ]
        need_zoho = bool(still_missing or browser_pending)
        if need_zoho:
            total_zoho = len(still_missing) + len(browser_pending)
            self.out.section_mid(f"Phase 3: Zoho Mail (searching {total_zoho} missing vendor(s))")
            client_id = self._get_secret(KEYCHAIN_SERVICE_ZOHO_CLIENT_ID, required=False)
            client_secret = self._get_secret(KEYCHAIN_SERVICE_ZOHO_CLIENT_SECRET, required=False)
            refresh_token = self._get_secret(KEYCHAIN_SERVICE_ZOHO_REFRESH_TOKEN, required=False)

            if client_id and client_secret and refresh_token:
                zoho = ZohoMailClient(
                    client_id=client_id, client_secret=client_secret, refresh_token=refresh_token
                )
                zoho.authenticate()
                zf = self.date_from.replace("-", "/")
                zt = self.cost_date_to.replace("-", "/")
                zoho_all_paths: list[Path] = []

                for vendor_cfg in still_missing:
                    email_pattern = vendor_cfg.email or vendor_cfg.pattern
                    result = zoho.search_and_download_vendor(
                        vendor_name=vendor_cfg.name,
                        search_term=email_pattern,
                        date_from=zf,
                        date_to=zt,
                        save_dir=self.costs_dir,
                        dry_run=self.dry_run,
                        link_re=vendor_cfg.link_re,
                    )
                    if result["found"]:
                        found_vendors[vendor_cfg.name] = "Zoho Mail"
                        total_cost_files += result["downloaded"]
                        zoho_all_paths.extend(result.get("saved_paths", []))
                        self.out.item(
                            f"✅ {vendor_cfg.name}: {result['downloaded']} PDF(s) from email"
                        )
                    else:
                        logger.info("Zoho Mail: no invoices found for %s", vendor_cfg.name)
                # Phase 3b: Browser-download vendors (e.g. Canva)
                for vendor_cfg in browser_pending:
                    email_pattern = vendor_cfg.email or vendor_cfg.pattern
                    invoice_ids = zoho.extract_invoice_ids(
                        search_term=email_pattern,
                        date_from=zf,
                        date_to=zt,
                        invoice_id_re=vendor_cfg.invoice_id_re or "",
                    )
                    if not invoice_ids:
                        logger.info("Zoho Mail: no invoice IDs found for %s", vendor_cfg.name)
                        continue

                    self.out.item(
                        f"🔍 {vendor_cfg.name}: found {len(invoice_ids)} invoice ID(s) in email"
                    )
                    saved_count = 0
                    for inv in invoice_ids:
                        inv_id = inv["id"]
                        tpl = vendor_cfg.invoice_file_tpl or "invoice-{id}.pdf"
                        filename = tpl.format(id=inv_id)
                        dest = self.costs_dir / filename
                        if dest.exists():
                            logger.info("Skipping %s — already exists", dest.name)
                            saved_count += 1
                            continue
                        if self.dry_run:
                            self.out.detail(f"  [dry-run] would download {inv_id}")
                            saved_count += 1
                            continue
                        try:
                            download_canva_invoice(inv_id, dest)
                            zoho_all_paths.append(dest)
                            saved_count += 1
                        except Exception as exc:
                            logger.error(
                                "Failed to download %s invoice %s: %s",
                                vendor_cfg.name,
                                inv_id,
                                exc,
                            )
                    if saved_count:
                        found_vendors[vendor_cfg.name] = "Zoho Mail + Browser"
                        total_cost_files += saved_count
                        self.out.item(f"✅ {vendor_cfg.name}: {saved_count} PDF(s) downloaded")
                # Candidate gate: validate issue dates
                if zoho_all_paths and not self.dry_run:
                    self.out.section_mid("Candidate gate: verifying invoice issue dates…")
                    accepted, rejected, unverified = validate_invoice_dates(
                        zoho_all_paths, self.date_from_obj, self.date_to_obj
                    )
                    if rejected:
                        deleted = delete_rejected(rejected)
                        total_cost_files -= len(deleted)
                        self.out.item(
                            f"🗑  {len(deleted)} PDF(s) rejected "
                            "(wrong date / duplicate / not an invoice)"
                        )
                    if unverified:
                        moved_uv = move_unverified(unverified)
                        total_cost_files -= len(moved_uv)
                        for p in moved_uv:
                            self.out.item(f"📁 {p.name} → _manual_check/")
                    if accepted:
                        self.out.item(
                            f"✅ {len(accepted)} PDF(s) confirmed within "
                            f"{self.date_from} – {self.date_to}"
                        )
            else:
                logger.info("Zoho OAuth not configured — skipping Phase 3")
                self.out.item("⏭  Zoho Mail not configured (run setup_zoho_oauth.py)")
        else:
            self.out.section_mid("Phase 3: Zoho Mail (all vendors found — skipped)")

        # Final vendor status
        final_missing = [
            v.name
            for v in EXPECTED_VENDORS
            if v.name not in found_vendors
            and not v.skip
            and v.name.lower() not in self.ignore_vendors
        ]
        self.report.cost_invoice_count = total_cost_files
        self.report.cost_found_vendors = found_vendors
        self.report.cost_missing_vendors = final_missing

        self.out.section_end("Result:")
        if final_missing:
            msg = f"Brak faktur kosztowych: {', '.join(final_missing)}. Uzupełnij lub pomiń w kolejnym miesiącu."
            self.report.warnings.append(msg)
            self.report.cost_missing_vendors = list(final_missing)
            self.out.warn(msg)
        self.out.detail("✅ All expected vendors accounted for!")
        self._upload_dir_to_blob(self.costs_dir, f"{self._blob_prefix}/koszty")
        self._mark_step_done("Cost invoices")

    def _step_5_bank_statement(self) -> None:
        self.out.step(5, "Verifying bank statement (PKO BP)")
        pko_files = [
            f
            for f in self.month_dir.rglob("*")
            if f.is_file()
            and ("wyciag" in f.name.lower() or "pko" in f.name.lower())
            and f.suffix.lower() == ".pdf"
        ]
        # Also check blob for files uploaded in step 1 (from preflight)
        if not pko_files and not self.report.bank_statement_found:
            try:
                blob_files = self.storage.list_files(self._blob_prefix + "/")
                pko_blobs = [
                    b
                    for b in blob_files
                    if ("wyciag" in b.key.lower() or "pko" in b.key.lower())
                    and b.key.lower().endswith(".pdf")
                ]
                if pko_blobs:
                    pko_files = [Path(pko_blobs[0].key)]
            except Exception:
                pass

        if pko_files:
            self.report.bank_statement_found = True
            self.out.ok(f"Bank statement found: {pko_files[0].name}")
        elif self.report.bank_statement_found:
            self.out.ok("Bank statement (found in pre-flight)")
        else:
            self.report.bank_statement_found = False
            msg = f"Wyciąg PKO BP za {self.year}-{self.month:02d} nie znaleziony. Pobierz z iPKO i wgraj do Inbox."
            self.report.warnings.append(msg)
            self.out.warn(msg)
        self._mark_step_done("Bank statement check")

    def _check_warnings_gate(self) -> None:
        """Block pipeline on warnings unless ``--ignore-warnings`` is set.

        When *ignore_warnings* is True the gate logs clearly but allows
        ZIP creation to proceed.  Email sending is always blocked when
        warnings exist (checked again in ``_step_7_email``).
        """
        if not self.report.warnings:
            return

        self.out.plain(f"\n  🚧 WARNINGS GATE: {len(self.report.warnings)} issue(s) detected:")
        for w in self.report.warnings:
            self.out.detail(f"• {w}")

        # Warnings never block ZIP — they only block email (checked in step 7).
        # Pipeline always completes with a report so the accountant sees full picture.
        self.out.warn(
            f"{len(self.report.warnings)} warning(s) — ZIP will be created. "
            "Email is blocked until warnings are resolved."
        )

    def _step_6_zip_archive(self) -> None:
        self.out.step(6, "Creating ZIP archive")
        if self.dry_run:
            zip_name = f"{self.month_pl}_{self.year}_HUMIO.zip"
            self.out.info(f"[DRY-RUN] Would create: {zip_name}")
            self._mark_step_done("ZIP archive (dry-run)")
            return
        try:
            blob_zip_key, count = create_month_archive_from_blob(
                self.storage, self._blob_prefix, self.month_pl, self.year
            )
            self.report.zip_path = Path(blob_zip_key)
            self.out.ok(f"ZIP created from blob → {blob_zip_key} ({count} files)")
        except Exception as exc:
            logger.warning("Blob ZIP failed, falling back to local: %s", exc)
            zip_path = create_month_archive(self.month_dir, self.month_pl, self.year)
            self.report.zip_path = zip_path
            blob_zip_key = f"{self._blob_prefix}/{zip_path.name}"
            try:
                self.storage.upload(zip_path, blob_zip_key)
                self.out.ok(f"ZIP uploaded to blob → {blob_zip_key}")
            except Exception as upload_exc:
                logger.warning("Could not upload ZIP to blob: %s", upload_exc)
                self.report.warnings.append(f"ZIP blob upload failed: {upload_exc}")
        self._mark_step_done("ZIP archive")

    def _step_7_email(self) -> None:
        self.out.step(7, "Sending email to accountant")
        if self.report.errors or self.report.warnings:
            issues = self.report.errors + self.report.warnings
            raise RuntimeError(
                f"Cannot send email — {len(issues)} issue(s) detected:\n"
                + "\n".join(f"  • {i}" for i in issues)
            )
        if self.dry_run:
            self.out.info(f"[DRY-RUN] Would send email to {ACCOUNTANT_EMAIL}")
            self._mark_step_done("Email (dry-run)")
            return
        smtp_pass = self._get_secret(KEYCHAIN_SERVICE_ZOHO_SMTP)
        svc = EmailService(smtp_password=smtp_pass or "")
        month_pl = POLISH_MONTHS[self.month].capitalize()
        subject = f"{COMPANY_BRAND} – Dokumenty księgowe – {month_pl} {self.year}"
        body = self._build_email_body()
        attachments = [self.report.zip_path] if self.report.zip_path else []
        svc.send_report(
            to_email=ACCOUNTANT_EMAIL, subject=subject, body=body, attachments=attachments
        )
        self.report.email_sent = True
        self.out.ok(f"Email sent → {ACCOUNTANT_EMAIL}")
        self._mark_step_done("Email")
        # Delete blob checkpoint — pipeline complete
        self.state.reset()
        self.out.ok("Pipeline checkpoint removed from blob")

    # ── Summary & helpers ────────────────────────────────────────────────────

    def _print_summary(self) -> None:
        r = self.report
        self.out.summary_header(f"SUMMARY – {self.month_en} {self.year}")
        self.out.summary_line("Sales invoices:", str(r.sales_invoice_count))
        self.out.summary_line("Sales gross total:", f"{r.sales_gross_total:,.2f} PLN")
        self.out.summary_line("Sales PDFs saved:", str(r.sales_pdfs_downloaded))
        self.out.summary_line("Cost invoices:", str(r.cost_invoice_count))
        if r.cost_found_vendors:
            for vname, vsrc in r.cost_found_vendors.items():
                self.out.plain(f"  Vendor: {vname:<20s} source: {vsrc}")
        if KSEF_ENABLED:
            self.out.summary_line("KSeF invoices:", str(r.ksef_count))
        self.out.summary_line(
            "Bank statement:", "✅ found" if r.bank_statement_found else "❌ MISSING"
        )
        self.out.summary_line("ZIP archive:", r.zip_path.name if r.zip_path else "—")
        self.out.summary_line("Email sent:", "✅" if r.email_sent else "—")
        if r.warnings:
            self.out.plain()
            self.out.plain("  ⚠️  WARNINGS:")
            for w in r.warnings:
                self.out.detail(f"• {w}")
        if r.errors:
            self.out.plain()
            self.out.plain("  ❌  ERRORS:")
            for e in r.errors:
                self.out.detail(f"• {e}")
        self.out.summary_footer(success=not r.errors)

    def _build_email_body(self) -> str:
        lines = [
            "Dzień dobry,",
            "",
            f"W załączeniu przesyłam dokumenty księgowe za {self.month_pl}.",
            "",
            "W razie pytań proszę o kontakt.",
            "",
            "Pozdrawiam,",
            "Piotr Gryzło",
        ]
        return "\n".join(lines)
