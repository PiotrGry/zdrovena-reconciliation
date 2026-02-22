"""
zdrovena.month_closing.orchestrator – Month Close Orchestrator
================================================================
Central controller that runs the full monthly accounting close pipeline:

  0. Pre-flight: check ~/Downloads for manual invoices, reports & bank statement
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
import os
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import keyring

from zdrovena.common import FakturowniaClient
from zdrovena.common.exceptions import MissingSecretError
from zdrovena.common.formatting import to_decimal
from zdrovena.month_closing.config import (
    ACCOUNTANT_EMAIL,
    BASE_DIR,
    COMPANY_BRAND,
    COST_INVOICE_OVERLAP_DAYS,
    ENGLISH_MONTHS,
    EXPECTED_VENDORS,
    FAKTUROWNIA_REPORTS,
    KEYCHAIN_ACCOUNT,
    KEYCHAIN_SERVICE_ZOHO_CLIENT_ID,
    KEYCHAIN_SERVICE_ZOHO_CLIENT_SECRET,
    KEYCHAIN_SERVICE_ZOHO_REFRESH_TOKEN,
    KEYCHAIN_SERVICE_ZOHO_SMTP,
    KSEF_ENABLED,
    POLISH_MONTHS,
    VendorConfig,
)
from zdrovena.month_closing.console import ConsoleReporter
from zdrovena.month_closing.download_watcher import interactive_download
from zdrovena.month_closing.email_service import EmailService
from zdrovena.month_closing.invoice_date_check import (
    delete_rejected,
    move_unverified,
    validate_invoice_dates,
)
from zdrovena.month_closing.ksef import KSeFClient
from zdrovena.month_closing.preflight import PreflightChecker
from zdrovena.month_closing.state import PipelineState
from zdrovena.month_closing.zip_service import create_month_archive
from zdrovena.month_closing.zoho_mail import ZohoMailClient
from zdrovena.month_closing.canva_downloader import download_canva_invoice

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

        self.report = CloseReport()
        self.state = PipelineState(self.month_dir)
        self.out = ConsoleReporter()

    @staticmethod
    def _get_secret(service: str, required: bool = True) -> str | None:
        # Env var: FAKTUROWNIA_API_TOKEN for service="fakturownia_api_token" etc.
        env_key = service.upper()
        value = os.environ.get(env_key) or keyring.get_password(service, KEYCHAIN_ACCOUNT)
        if not value and required:
            raise MissingSecretError(service, KEYCHAIN_ACCOUNT)
        return value

    def _skip_if_done(self, step_name: str) -> bool:
        if self.state.is_done(step_name):
            self.out.skip(f"{step_name} (already done — skipping)")
            self.report.steps_completed.append(step_name)
            return True
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
        )
        pf = checker.run()
        self.report.bank_statement_found = pf.bank_statement_found
        self.report.warnings.extend(pf.warnings)
        self._preflight_checker = checker

        # Try interactive download for manual vendors that have a fallback_url
        watchable = [
            v for v in pf.missing_vendors
            if v.fallback_url and v.download_glob
        ]
        if watchable and not self.dry_run:
            if self.non_interactive:
                names = [v.name for v in watchable]
                raise RuntimeError(
                    f"--non-interactive: required manual downloads missing: "
                    f"{', '.join(names)}. Place files in ~/Downloads and retry."
                )
            resolved = self._interactive_download(watchable, checker)
            # Remove resolved vendors from missing list
            for v in resolved:
                if v in pf.missing_vendors:
                    pf.missing_vendors.remove(v)

        blockers = checker.build_blockers()
        if blockers:
            self.out.blocker_box(blockers)
            self.out.plain()
            self.out.plain("  Place all files in: ~/Downloads")
            self.out.plain(
                f"  Then rerun:  zdrovena close {self.year}-{self.month:02d}"
                f"{' --dry-run' if self.dry_run else ''}"
            )
            self.out.plain()
            raise SystemExit(1)
        self._mark_step_done("Pre-flight")

    def _interactive_download(
        self,
        vendors: list[VendorConfig],
        checker: PreflightChecker,
    ) -> list[VendorConfig]:
        """Open fallback URLs and watch ~/Downloads for matching files."""

        def _on_match(vendor: VendorConfig, path: Path) -> None:
            checker.result.matches.append((vendor, path))

        results = interactive_download(vendors, self.out, on_match=_on_match)
        return [v for v, _ in results]

    def _step_1_create_folders(self) -> None:
        self.out.step(1, "Creating folder structure")
        self.sales_dir.mkdir(parents=True, exist_ok=True)
        self.costs_dir.mkdir(parents=True, exist_ok=True)
        logger.info("Folders ready: %s", self.month_dir)
        checker = getattr(self, "_preflight_checker", None)
        if checker is not None:
            checker.copy_to_folders(self.month_dir, self.costs_dir)
        self._mark_step_done("Folder structure")
        self.out.ok(str(self.month_dir))

    def _step_2_sales_invoices(self) -> None:
        self.out.step(2, "Downloading sales invoices (Fakturownia)")
        if self._skip_if_done("Sales invoices"):
            return
        client = FakturowniaClient.from_keyring()
        invoices = client.fetch_sales_invoices(self.date_from, self.date_to)
        self.report.sales_invoice_count = len(invoices)
        self.report.sales_gross_total = sum(
            to_decimal(inv.get("price_gross", 0)) for inv in invoices
        )
        self._sales_invoices = invoices
        if not invoices:
            raise RuntimeError(
                f"No sales invoices found for {self.date_from} – {self.date_to}. "
                "Check Fakturownia for the correct date range."
            )
        saved = client.download_all_pdfs(invoices, self.sales_dir, dry_run=self.dry_run)
        self.report.sales_pdfs_downloaded = len(saved)
        self.out.ok(
            f"{len(invoices)} invoice(s), gross total: {self.report.sales_gross_total:,.2f} PLN"
        )
        self._mark_step_done("Sales invoices")

    def _step_3_jpk_reports(self) -> None:
        self.out.step(3, "Verifying JPK and VAT reports")
        all_found = True
        for rpt in FAKTUROWNIA_REPORTS:
            dest = self.month_dir / rpt["dest_name"]
            if dest.exists():
                self.out.ok(f"{rpt['name']}: {dest.name}")
            else:
                self.out.warn(f"{rpt['name']}: MISSING — {dest}")
                self.report.warnings.append(f"{rpt['name']} not found in month folder.")
                all_found = False
        if all_found:
            self._mark_step_done("JPK & VAT reports")
        else:
            raise RuntimeError(
                "JPK/VAT reports incomplete. Download missing reports from "
                "Fakturownia UI and place them in the month folder."
            )

    def _step_4_cost_invoices(self) -> None:
        self.out.step(4, "Collecting cost invoices")
        if self._skip_if_done("Cost invoices"):
            return
        self.out.info(
            f"📅 Issue date: {self.date_from} → {self.date_to} "
            f"(Zoho email search extends to {self.cost_date_to})"
        )
        found_vendors: dict[str, str] = {}
        total_cost_files = 0

        # Phase 1: KSeF
        ksef_numbers: set[str] = set()
        if KSEF_ENABLED:
            self.out.section_start("Phase 1: KSeF verification")
            ksef_client = KSeFClient()
            ksef_client.authenticate()
            ksef_invoices = ksef_client.query_purchase_invoices(self.date_from, self.date_to)
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
        fakt_invoices = fakt_client.fetch_cost_invoices(self.date_from, self.date_to)
        self._cost_invoices = fakt_invoices

        # Phase 2a: Cross-verify KSeF vs Fakturownia
        if KSEF_ENABLED and ksef_numbers:
            fakt_gov_ids = {inv.get("gov_id", "") for inv in fakt_invoices if inv.get("gov_id")}
            missing_in_fakt = ksef_numbers - fakt_gov_ids
            if missing_in_fakt:
                for knum in sorted(missing_in_fakt):
                    self.out.item(f"❌ KSeF invoice NOT in Fakturownia: {knum}")
                raise RuntimeError(
                    f"{len(missing_in_fakt)} KSeF invoice(s) missing in Fakturownia: "
                    f"{', '.join(sorted(missing_in_fakt))}. "
                    "Fakturownia auto-fetches every 15 min — wait and retry, or "
                    "click 'Pobierz faktury z KSeF' in Fakturownia UI."
                )
            self.out.item(
                f"✅ KSeF cross-check: all {len(ksef_numbers)} invoice(s) found in Fakturownia"
            )

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
        ]
        browser_pending = [
            v
            for v in EXPECTED_VENDORS
            if v.browser_download
            and v.name not in found_vendors
            and v.invoice_id_re
            and not v.skip
        ]
        need_zoho = bool(still_missing or browser_pending)
        if need_zoho:
            total_zoho = len(still_missing) + len(browser_pending)
            self.out.section_mid(
                f"Phase 3: Zoho Mail (searching {total_zoho} missing vendor(s))"
            )
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
                        invoice_id_re=vendor_cfg.invoice_id_re,
                    )
                    if not invoice_ids:
                        logger.info(
                            "Zoho Mail: no invoice IDs found for %s", vendor_cfg.name
                        )
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
                            logger.info(
                                "Skipping %s — already exists", dest.name
                            )
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
                                vendor_cfg.name, inv_id, exc,
                            )
                    if saved_count:
                        found_vendors[vendor_cfg.name] = "Zoho Mail + Browser"
                        total_cost_files += saved_count
                        self.out.item(
                            f"✅ {vendor_cfg.name}: {saved_count} PDF(s) downloaded"
                        )
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
            v.name for v in EXPECTED_VENDORS if v.name not in found_vendors and not v.skip
        ]
        self.report.cost_invoice_count = total_cost_files
        self.report.cost_found_vendors = found_vendors
        self.report.cost_missing_vendors = final_missing

        self.out.section_end("Result:")
        if final_missing:
            raise RuntimeError(
                f"Missing cost vendors: {', '.join(final_missing)}. "
                "All expected vendors must be accounted for before proceeding."
            )
        self.out.detail("✅ All expected vendors accounted for!")
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
        if pko_files:
            self.report.bank_statement_found = True
            self.out.ok(f"Bank statement found: {pko_files[0].name}")
        elif self.report.bank_statement_found:
            self.out.ok("Bank statement (found in pre-flight)")
        else:
            self.report.bank_statement_found = False
            raise RuntimeError(
                f"Bank statement (PKO BP) for {self.year}-{self.month:02d} not found. "
                f"Download from iPKO and place in {self.month_dir}"
            )
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

        if self.ignore_warnings:
            self.out.warn(
                "--ignore-warnings: continuing to ZIP despite warnings. "
                "Email sending is still blocked."
            )
            return

        raise RuntimeError(
            f"Aborting: {len(self.report.warnings)} warning(s) detected. "
            "Fix all issues or rerun with --ignore-warnings."
        )

    def _step_6_zip_archive(self) -> None:
        self.out.step(6, "Creating ZIP archive")
        if self.dry_run:
            zip_name = f"{self.month_pl}_{self.year}_HUMIO.zip"
            self.out.info(f"[DRY-RUN] Would create: {zip_name}")
            self._mark_step_done("ZIP archive (dry-run)")
            return
        zip_path = create_month_archive(self.month_dir, self.month_pl, self.year)
        self.report.zip_path = zip_path
        self.out.ok(zip_path.name)
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
        svc = EmailService(smtp_password=smtp_pass)
        subject = (
            f"{COMPANY_BRAND} \u2013 Accounting documents \u2013 "
            f"{self.month_en} {self.year}"
        )
        body = self._build_email_body()
        attachments = [self.report.zip_path] if self.report.zip_path else []
        svc.send_report(
            to_email=ACCOUNTANT_EMAIL, subject=subject, body=body, attachments=attachments
        )
        self.report.email_sent = True
        self.out.ok(f"Email sent → {ACCOUNTANT_EMAIL}")
        self._mark_step_done("Email")

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
