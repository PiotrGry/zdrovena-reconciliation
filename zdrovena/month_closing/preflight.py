"""
zdrovena.month_closing.preflight – Pre-flight Checker
=======================================================
Ensures all manually-downloaded documents (vendor invoices, bank statement,
Fakturownia reports) are present before the pipeline starts.
"""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from zdrovena.month_closing.config import (
    DOWNLOAD_WATCH_DIR,
    EXPECTED_VENDORS,
    FAKTUROWNIA_REPORTS,
    VendorConfig,
)
from zdrovena.month_closing.zoho_mail import ZohoMailClient

logger = logging.getLogger("zdrovena.month_closing.preflight")


@dataclass
class PreflightResult:
    matches: list[tuple[VendorConfig | dict, Path]] = field(default_factory=list)
    missing_vendors: list[VendorConfig] = field(default_factory=list)
    missing_reports: list[dict] = field(default_factory=list)
    bank_statement_found: bool = False
    warnings: list[str] = field(default_factory=list)


class PreflightChecker:
    def __init__(
        self,
        year: int,
        month: int,
        month_dir: Path,
        date_from: str,
        date_to: str,
        cost_date_to: str,
        dry_run: bool,
        get_secret: object,
    ) -> None:
        self.year = year
        self.month = month
        self.month_dir = month_dir
        self.date_from = date_from
        self.date_to = date_to
        self.cost_date_to = cost_date_to
        self.dry_run = dry_run
        self._get_secret = get_secret
        self.result = PreflightResult()

    def run(self) -> PreflightResult:
        manual_vendors = [v for v in EXPECTED_VENDORS if v.download_glob]

        if manual_vendors:
            print("  ┌─ Manual invoices")
            try:
                self._check_vendors(manual_vendors)
            except Exception as exc:
                logger.warning("Pre-flight vendor check failed: %s", exc, exc_info=True)
                print(f"  │  ⚠️  Pre-flight vendor check failed: {exc}")
                for v in manual_vendors:
                    if not any(m is v for m, _ in self.result.matches):
                        self.result.missing_vendors.append(v)
        else:
            print("  ┌─ No manual-download vendors configured")

        try:
            self._check_bank_statement()
        except Exception as exc:
            logger.warning("Pre-flight bank statement check failed: %s", exc)
            print(f"  ⚠️  Bank statement check error: {exc}")
            self.result.bank_statement_found = False

        try:
            self._check_reports()
        except Exception as exc:
            logger.warning("Pre-flight report check failed: %s", exc)
            print(f"  ⚠️  Report check error: {exc}")

        return self.result

    def build_blockers(self) -> list[str]:
        blockers: list[str] = []
        for v in self.result.missing_vendors:
            url = v.fallback_url or ""
            blockers.append(f"  ❌ {v.name}: {url}" if url else f"  ❌ {v.name}")
        if not self.result.bank_statement_found:
            blockers.append(
                f"  ❌ PKO BP bank statement for {self.year}-{self.month:02d} → download from iPKO"
            )
        for rpt in self.result.missing_reports:
            url = rpt.get("url", "")
            blockers.append(f"  ❌ {rpt['name']}: {url}" if url else f"  ❌ {rpt['name']}")
        return blockers

    def copy_to_folders(self, month_dir: Path, costs_dir: Path) -> None:
        for vendor_cfg, src_pdf in self.result.matches:
            name = vendor_cfg.name if isinstance(vendor_cfg, VendorConfig) else vendor_cfg["name"]
            dest_name = vendor_cfg.get("dest_name") if isinstance(vendor_cfg, dict) else None
            if name == "PKO BP":
                dest_dir = month_dir
                safe_name = src_pdf.name
            elif dest_name:
                dest_dir = month_dir
                safe_name = dest_name
            else:
                dest_dir = costs_dir
                safe_name = f"{name.replace(' ', '_')}_{src_pdf.name}"
            target = dest_dir / safe_name
            if target.exists():
                logger.info("Pre-flight: %s already exists — skipping", target)
                continue
            if not self.dry_run:
                shutil.copy2(src_pdf, target)
                logger.info("Pre-flight: copied %s → %s", src_pdf, target)
            dest_label = dest_dir.name + "/"
            print(f"  📥 {name}: copied {src_pdf.name} → {dest_label}")

    # ── Private helpers ──────────────────────────────────────────────────────

    def _check_vendors(self, manual_vendors: list[VendorConfig]) -> None:
        watch_dir = DOWNLOAD_WATCH_DIR
        if not watch_dir.exists():
            for v in manual_vendors:
                self.result.missing_vendors.append(v)
                print(f"  │  ⚠️  {v.name}: ~/Downloads not found")
            return

        zoho: ZohoMailClient | None = None
        try:
            from zdrovena.month_closing.config import (
                KEYCHAIN_SERVICE_ZOHO_CLIENT_ID,
                KEYCHAIN_SERVICE_ZOHO_CLIENT_SECRET,
                KEYCHAIN_SERVICE_ZOHO_REFRESH_TOKEN,
            )

            client_id = self._get_secret(KEYCHAIN_SERVICE_ZOHO_CLIENT_ID, required=False)
            client_secret = self._get_secret(KEYCHAIN_SERVICE_ZOHO_CLIENT_SECRET, required=False)
            refresh_token = self._get_secret(KEYCHAIN_SERVICE_ZOHO_REFRESH_TOKEN, required=False)
            if client_id and client_secret and refresh_token:
                zoho = ZohoMailClient(
                    client_id=client_id, client_secret=client_secret, refresh_token=refresh_token
                )
                zoho.authenticate()
        except Exception as exc:
            logger.warning("Zoho init for pre-flight failed: %s", exc)
            print(f"  │  ℹ️  Zoho unavailable — using glob patterns only ({exc})")

        zf = self.date_from.replace("-", "/")
        zt = self.cost_date_to.replace("-", "/")

        for vendor_cfg in manual_vendors:
            try:
                found = self._find_vendor(vendor_cfg, watch_dir, zoho, zf, zt)
                if not found:
                    self.result.missing_vendors.append(vendor_cfg)
            except Exception as exc:
                logger.warning("Pre-flight %s failed: %s", vendor_cfg.name, exc)
                print(f"  │  ⚠️  {vendor_cfg.name}: check failed — {exc}")
                self.result.missing_vendors.append(vendor_cfg)

    def _find_vendor(self, vendor_cfg, watch_dir, zoho, date_from, date_to) -> bool:
        name = vendor_cfg.name
        invoice_id_re = vendor_cfg.invoice_id_re
        file_tpl = vendor_cfg.invoice_file_tpl
        email_term = vendor_cfg.email or vendor_cfg.pattern
        glob_pat = vendor_cfg.download_glob or ""
        fallback_url = vendor_cfg.fallback_url or ""

        expected_files: list[str] = []
        email_urls: list[str] = []

        if zoho and invoice_id_re and file_tpl and email_term:
            try:
                invoice_ids = zoho.extract_invoice_ids(
                    search_term=email_term,
                    date_from=date_from,
                    date_to=date_to,
                    invoice_id_re=invoice_id_re,
                )
                for inv in invoice_ids:
                    fname = file_tpl.format(id=inv["id"])
                    expected_files.append(fname)
                    if inv.get("url"):
                        email_urls.append(inv["url"])
            except Exception as exc:
                logger.warning("Zoho invoice ID extraction for %s failed: %s", name, exc)

        if expected_files:
            for fname in expected_files:
                target = watch_dir / fname
                if target.is_file():
                    self.result.matches.append((vendor_cfg, target))
                    print(f"  │  ✅ {name}: found {target.name} (from email)")
                    return True
            print(f"  │  ⚠️  {name}: expected {', '.join(expected_files)} — not in ~/Downloads")
            if email_urls:
                for url in email_urls:
                    print(f"  │     🔗 {url}")
            elif fallback_url:
                print(f"  │     🔗 Download from: {fallback_url}")
            return False

        matches = sorted(watch_dir.glob(glob_pat), key=lambda f: f.stat().st_mtime, reverse=True)
        if matches:
            newest = matches[0]
            self.result.matches.append((vendor_cfg, newest))
            print(f"  │  ✅ {name}: found {newest.name} (glob match)")
            return True

        print(f"  │  ⚠️  {name}: no matching PDF in ~/Downloads")
        if fallback_url:
            print(f"  │     🔗 Download from: {fallback_url}")
        return False

    def _check_bank_statement(self) -> None:
        if self.month_dir.exists():
            pko_files = [
                f
                for f in self.month_dir.rglob("*")
                if f.is_file()
                and ("wyciag" in f.name.lower() or "pko" in f.name.lower())
                and f.suffix.lower() == ".pdf"
            ]
            if pko_files:
                self.result.bank_statement_found = True
                print(f"  └─ ✅ Bank statement: {pko_files[0].name} (in month folder)")
                return

        watch_dir = DOWNLOAD_WATCH_DIR
        if watch_dir.exists():
            pko_downloads = sorted(
                (f for f in watch_dir.glob("Wyciag_na_zadanie_*.pdf") if f.is_file()),
                key=lambda f: f.stat().st_mtime,
                reverse=True,
            )
            matching = [
                f for f in pko_downloads if pko_matches_month(f.name, self.year, self.month)
            ]
            if matching:
                best = matching[0]
                self.result.matches.append(
                    ({"name": "PKO BP", "download_glob": "Wyciag_na_zadanie_*.pdf"}, best)
                )
                self.result.bank_statement_found = True
                print(f"  └─ ✅ Bank statement: {best.name} (in ~/Downloads)")
                return
            if pko_downloads:
                wrong = pko_downloads[0]
                print(f"  └─ ⚠️  Found {wrong.name} but it's not for {self.year}-{self.month:02d}")

        self.result.bank_statement_found = False
        self.result.warnings.append(
            f"Bank statement (PKO BP) for {self.year}-{self.month:02d} not found. "
            "Download from iPKO and place in ~/Downloads."
        )
        if self.month == 12:
            gen_year, gen_month = self.year + 1, 1
        else:
            gen_year, gen_month = self.year, self.month + 1
        print(f"  └─ ⚠️  No PKO BP bank statement for {self.year}-{self.month:02d}")
        print(f"     Download from iPKO → filename: Wyciag_na_zadanie_*_{gen_year}{gen_month:02d}*.pdf")

    def _check_reports(self) -> None:
        watch_dir = DOWNLOAD_WATCH_DIR
        print("  ┌─ Fakturownia reports")
        for rpt in FAKTUROWNIA_REPORTS:
            dest = self.month_dir / rpt["dest_name"]
            if dest.exists():
                print(f"  │  ✅ {rpt['name']}: {dest.name} (in month folder)")
                continue
            if not watch_dir.exists():
                self.result.missing_reports.append(rpt)
                print(f"  │  ⚠️  {rpt['name']}: ~/Downloads not found")
                continue
            matches = sorted(
                watch_dir.glob(rpt["glob"]), key=lambda f: f.stat().st_mtime, reverse=True
            )
            if matches:
                newest = matches[0]
                self.result.matches.append(
                    ({"name": rpt["name"], "dest_name": rpt["dest_name"]}, newest)
                )
                print(f"  │  ✅ {rpt['name']}: found {newest.name}")
            else:
                self.result.missing_reports.append(rpt)
                print(f"  │  ⚠️  {rpt['name']}: not found in ~/Downloads")
                if rpt.get("url"):
                    print(f"  │     🔗 {rpt['url']}")
        print("  └─")


def pko_matches_month(filename: str, year: int, month: int) -> bool:
    m = re.search(r"_(202\d)(\d{2})(\d{2})\d+\.pdf$", filename, re.IGNORECASE)
    if not m:
        return False
    try:
        file_year = int(m.group(1))
        file_month = int(m.group(2))
        if month == 12:
            expected_year = year + 1
            expected_month = 1
        else:
            expected_year = year
            expected_month = month + 1
        return file_year == expected_year and file_month == expected_month
    except (ValueError, IndexError):
        return False
