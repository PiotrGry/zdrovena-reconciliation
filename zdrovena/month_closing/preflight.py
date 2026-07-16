"""
zdrovena.month_closing.preflight – Pre-flight Checker
=======================================================
Ensures all manually-downloaded documents (vendor invoices, bank statement,
Fakturownia reports) are present before the pipeline starts.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import re
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from zdrovena.common.storage import BlobFile, StorageService

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
        get_secret: Callable[..., str | None],
        no_browser: bool = False,
        storage: StorageService | None = None,
        blob_inbox_prefix: str = "faktury/inbox",
        out_fn: Callable[[str], None] | None = None,
    ) -> None:
        self.year = year
        self.month = month
        self.month_dir = month_dir
        self.date_from = date_from
        self.date_to = date_to
        self.cost_date_to = cost_date_to
        self.dry_run = dry_run
        self._get_secret = get_secret
        self.no_browser = no_browser
        self._storage = storage
        self._blob_inbox_prefix = blob_inbox_prefix
        self._blob_downloads: list[tuple[str, Path]] = []
        self._out: Callable[[str], None] = out_fn if out_fn is not None else print
        self.result = PreflightResult()

    def run(self) -> PreflightResult:
        manual_vendors = [v for v in EXPECTED_VENDORS if v.download_glob]

        if manual_vendors:
            self._out("  ┌─ Manual invoices")
            try:
                self._check_vendors(manual_vendors)
            except Exception as exc:
                logger.warning("Pre-flight vendor check failed: %s", exc, exc_info=True)
                self._out(f"  │  ⚠️  Pre-flight vendor check failed: {exc}")
                for v in manual_vendors:
                    if not any(m is v for m, _ in self.result.matches):
                        self.result.missing_vendors.append(v)
        else:
            self._out("  ┌─ No manual-download vendors configured")

        try:
            self._check_bank_statement()
        except Exception as exc:
            logger.warning("Pre-flight bank statement check failed: %s", exc)
            self._out(f"  ⚠️  Bank statement check error: {exc}")
            self.result.bank_statement_found = False

        try:
            self._check_reports()
        except Exception as exc:
            logger.warning("Pre-flight report check failed: %s", exc)
            self._out(f"  ⚠️  Report check error: {exc}")

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
        return blockers

    def copy_to_folders(self, month_dir: Path, costs_dir: Path) -> None:
        tmp_to_blob: dict[Path, str] = {tmp: key for key, tmp in self._blob_downloads}

        for vendor_cfg, src_pdf in self.result.matches:
            name = vendor_cfg.name if isinstance(vendor_cfg, VendorConfig) else vendor_cfg["name"]
            dest_name = vendor_cfg.get("dest_name") if isinstance(vendor_cfg, dict) else None
            blob_filename = (
                Path(tmp_to_blob[src_pdf]).name if src_pdf in tmp_to_blob else src_pdf.name
            )
            if name == "PKO BP":
                dest_dir = month_dir
                safe_name = blob_filename
            elif dest_name:
                dest_dir = month_dir
                safe_name = dest_name
            else:
                dest_dir = costs_dir
                safe_name = f"{name.replace(' ', '_')}_{blob_filename}"
            target = dest_dir / safe_name
            if target.exists():
                logger.info("Pre-flight: %s already exists — skipping", target)
                self._cleanup_blob_tmp(src_pdf, tmp_to_blob)
                continue
            if not self.dry_run:
                shutil.copy2(src_pdf, target)
                logger.info("Pre-flight: copied %s → %s", src_pdf, target)
                self._cleanup_blob_tmp(src_pdf, tmp_to_blob)
            dest_label = dest_dir.name + "/"
            self._out(f"  📥 {name}: copied {blob_filename} → {dest_label}")

    # ── Private helpers ──────────────────────────────────────────────────────

    def _cleanup_blob_tmp(self, src_pdf: Path, tmp_to_blob: dict[Path, str]) -> None:
        blob_key = tmp_to_blob.get(src_pdf)
        if blob_key and self._storage:
            try:
                self._storage.delete(blob_key)
                logger.info("Pre-flight: deleted blob %s after copy", blob_key)
            except Exception as exc:
                logger.warning("Pre-flight: could not delete blob %s: %s", blob_key, exc)
        if src_pdf in tmp_to_blob:
            src_pdf.unlink(missing_ok=True)

    def _list_blob_inbox(self) -> list[BlobFile]:
        if not self._storage:
            return []
        prefix = self._blob_inbox_prefix.rstrip("/") + "/"
        try:
            return self._storage.list_files(prefix)
        except Exception as exc:
            logger.warning("Could not list blob inbox %s: %s", prefix, exc)
            return []

    def _download_blob_to_tmp(self, key: str) -> Path | None:
        if not self._storage:
            return None
        try:
            suffix = Path(key).suffix
            fd, tmp_name = tempfile.mkstemp(suffix=suffix)
            os.close(fd)
            tmp = Path(tmp_name)
            self._storage.download(key, tmp)
            self._blob_downloads.append((key, tmp))
            return tmp
        except Exception as exc:
            logger.warning("Could not download blob %s: %s", key, exc)
            return None

    def _init_zoho(self) -> ZohoMailClient | None:
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
                return zoho
        except Exception as exc:
            logger.warning("Zoho init for pre-flight failed: %s", exc)
            self._out(f"  │  ℹ️  Zoho unavailable — using glob patterns only ({exc})")
        return None

    def _check_vendors(self, manual_vendors: list[VendorConfig]) -> None:
        watch_dir = DOWNLOAD_WATCH_DIR
        zoho = self._init_zoho()
        zf = self.date_from.replace("-", "/")
        zt = self.cost_date_to.replace("-", "/")
        prefer_scoped_blob = bool(
            self._storage and self._blob_inbox_prefix.rstrip("/") != "faktury/inbox"
        )

        if prefer_scoped_blob or not watch_dir.exists():
            if self._storage:
                blob_files = self._list_blob_inbox()
                for vendor_cfg in manual_vendors:
                    try:
                        found = self._find_vendor_in_blob(vendor_cfg, blob_files, zoho, zf, zt)
                        if not found:
                            self.result.missing_vendors.append(vendor_cfg)
                    except Exception as exc:
                        logger.warning("Pre-flight blob %s failed: %s", vendor_cfg.name, exc)
                        self._out(f"  │  ⚠️  {vendor_cfg.name}: blob check failed — {exc}")
                        self.result.missing_vendors.append(vendor_cfg)
            else:
                for v in manual_vendors:
                    self.result.missing_vendors.append(v)
                    self._out(f"  │  ⚠️  {v.name}: inbox/ not found")
            return

        for vendor_cfg in manual_vendors:
            try:
                found = self._find_vendor(vendor_cfg, watch_dir, zoho, zf, zt)
                if not found:
                    self.result.missing_vendors.append(vendor_cfg)
            except Exception as exc:
                logger.warning("Pre-flight %s failed: %s", vendor_cfg.name, exc)
                self._out(f"  │  ⚠️  {vendor_cfg.name}: check failed — {exc}")
                self.result.missing_vendors.append(vendor_cfg)

    def _find_vendor_in_blob(self, vendor_cfg, blob_files, zoho, date_from, date_to) -> bool:
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
            blob_names = {Path(b.key).name: b.key for b in blob_files}
            for fname in expected_files:
                if fname in blob_names:
                    tmp = self._download_blob_to_tmp(blob_names[fname])
                    if tmp:
                        self.result.matches.append((vendor_cfg, tmp))
                        self._out(f"  │  ✅ {name}: found {fname} (from blob)")
                        return True
            # Exact file not found — try glob fallback before giving up.
            # Handles the case where the PDF is present but with a slightly
            # different name than what Zoho returned (e.g. manual upload with
            # a different invoice ID suffix).
            if glob_pat:
                glob_matches = sorted(
                    [b for b in blob_files if fnmatch.fnmatch(Path(b.key).name, glob_pat)],
                    key=lambda b: b.last_modified,
                    reverse=True,
                )
                if glob_matches:
                    newest = glob_matches[0]
                    tmp = self._download_blob_to_tmp(newest.key)
                    if tmp:
                        self.result.matches.append((vendor_cfg, tmp))
                        self._out(f"  │  ✅ {name}: found {Path(newest.key).name} (glob fallback)")
                        return True
            self._out(f"  │  ⚠️  {name}: expected {', '.join(expected_files)} — not in blob inbox/")
            if email_urls:
                for url in email_urls:
                    self._out(f"  │     🔗 {url}")
            elif fallback_url:
                self._out(f"  │     🔗 Download from: {fallback_url}")
            return False

        if glob_pat:
            matches = sorted(
                [b for b in blob_files if fnmatch.fnmatch(Path(b.key).name, glob_pat)],
                key=lambda b: b.last_modified,
                reverse=True,
            )
            if matches:
                newest = matches[0]
                tmp = self._download_blob_to_tmp(newest.key)
                if tmp:
                    self.result.matches.append((vendor_cfg, tmp))
                    self._out(f"  │  ✅ {name}: found {Path(newest.key).name} (from blob)")
                    return True

        self._out(f"  │  ⚠️  {name}: no matching PDF in blob inbox/")
        if fallback_url:
            self._out(f"  │     🔗 Download from: {fallback_url}")
        return False

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
                    self._out(f"  │  ✅ {name}: found {target.name} (from email)")
                    return True
            self._out(f"  │  ⚠️  {name}: expected {', '.join(expected_files)} — not in inbox/")
            if email_urls:
                for url in email_urls:
                    self._out(f"  │     🔗 {url}")
            elif fallback_url:
                self._out(f"  │     🔗 Download from: {fallback_url}")
            return False

        matches = sorted(watch_dir.glob(glob_pat), key=lambda f: f.stat().st_mtime, reverse=True)
        if matches:
            newest = matches[0]
            self.result.matches.append((vendor_cfg, newest))
            self._out(f"  │  ✅ {name}: found {newest.name} (glob match)")
            return True

        self._out(f"  │  ⚠️  {name}: no matching PDF in inbox/")
        if fallback_url:
            self._out(f"  │     🔗 Download from: {fallback_url}")
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
                self._out(f"  └─ ✅ Bank statement: {pko_files[0].name} (in month folder)")
                return

        watch_dir = DOWNLOAD_WATCH_DIR
        prefer_scoped_blob = bool(
            self._storage and self._blob_inbox_prefix.rstrip("/") != "faktury/inbox"
        )
        if watch_dir.exists() and not prefer_scoped_blob:
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
                self._out(f"  └─ ✅ Bank statement: {best.name} (in inbox/)")
                return
            if pko_downloads:
                wrong = pko_downloads[0]
                self._out(
                    f"  └─ ⚠️  Found {wrong.name} but it's not for {self.year}-{self.month:02d}"
                )
        elif self._storage:
            blob_files = self._list_blob_inbox()
            pko_blobs = sorted(
                [
                    b
                    for b in blob_files
                    if fnmatch.fnmatch(Path(b.key).name, "Wyciag_na_zadanie_*.pdf")
                ],
                key=lambda b: b.last_modified,
                reverse=True,
            )
            matching_blobs = [
                b for b in pko_blobs if pko_matches_month(Path(b.key).name, self.year, self.month)
            ]
            if matching_blobs:
                best = matching_blobs[0]
                tmp = self._download_blob_to_tmp(best.key)
                if tmp:
                    self.result.matches.append(
                        ({"name": "PKO BP", "download_glob": "Wyciag_na_zadanie_*.pdf"}, tmp)
                    )
                    self.result.bank_statement_found = True
                    self._out(f"  └─ ✅ Bank statement: {Path(best.key).name} (from blob)")
                    return
            if pko_blobs:
                wrong = pko_blobs[0]
                self._out(
                    f"  └─ ⚠️  Found {Path(wrong.key).name} but it's not for "
                    f"{self.year}-{self.month:02d}"
                )

        self.result.bank_statement_found = False
        self.result.warnings.append(
            f"Bank statement (PKO BP) for {self.year}-{self.month:02d} not found. "
            "Download from iPKO and place in inbox/."
        )
        if self.month == 12:
            gen_year, gen_month = self.year + 1, 1
        else:
            gen_year, gen_month = self.year, self.month + 1
        self._out(f"  └─ ⚠️  No PKO BP bank statement for {self.year}-{self.month:02d}")
        self._out(
            f"     Download from iPKO → filename: Wyciag_na_zadanie_*_{gen_year}{gen_month:02d}*.pdf"
        )

    def _check_reports(self) -> None:
        watch_dir = DOWNLOAD_WATCH_DIR
        self._out("  ┌─ Fakturownia reports")
        prefer_scoped_blob = bool(
            self._storage and self._blob_inbox_prefix.rstrip("/") != "faktury/inbox"
        )
        blob_files = (
            self._list_blob_inbox()
            if (prefer_scoped_blob or not watch_dir.exists()) and self._storage
            else []
        )

        missing: list[dict] = []
        for rpt in FAKTUROWNIA_REPORTS:
            dest = self.month_dir / rpt["dest_name"]
            if dest.exists():
                self._out(f"  │  ✅ {rpt['name']}: {dest.name} (in month folder)")
                continue
            if watch_dir.exists() and not prefer_scoped_blob:
                matches = sorted(
                    watch_dir.glob(rpt["glob"]),
                    key=lambda f: f.stat().st_mtime,
                    reverse=True,
                )
                if matches:
                    newest = matches[0]
                    self.result.matches.append(
                        ({"name": rpt["name"], "dest_name": rpt["dest_name"]}, newest)
                    )
                    self._out(f"  │  ✅ {rpt['name']}: found {newest.name}")
                    continue
            elif blob_files:
                blob_matches = sorted(
                    [b for b in blob_files if fnmatch.fnmatch(Path(b.key).name, rpt["glob"])],
                    key=lambda b: b.last_modified,
                    reverse=True,
                )
                if blob_matches:
                    newest_blob = blob_matches[0]
                    tmp = self._download_blob_to_tmp(newest_blob.key)
                    if tmp:
                        self.result.matches.append(
                            ({"name": rpt["name"], "dest_name": rpt["dest_name"]}, tmp)
                        )
                        self._out(
                            f"  │  ✅ {rpt['name']}: found {Path(newest_blob.key).name} (from blob)"
                        )
                        continue
            missing.append(rpt)

        # Remaining missing reports are warnings only — auto-download happens in orchestrator step 3
        for rpt in missing:
            self.result.missing_reports.append(rpt)
            self._out(f"  │  ⚠️  {rpt['name']}: not found in inbox/")
            if rpt.get("url"):
                self._out(f"  │     🔗 {rpt['url']}")
        self._out("  └─")


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
