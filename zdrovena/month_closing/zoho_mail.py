"""
zdrovena.month_closing.zoho_mail – Zoho Mail REST API Client
===============================================================
Searches for cost invoices (PDF attachments) from expected vendors
within a given month range, with hash-based deduplication.
Uses the Zoho Mail REST API (OAuth 2.0).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import UTC
from pathlib import Path
from typing import Any

import requests

from zdrovena.month_closing.config import ZOHO_ACCOUNTS_URL, ZOHO_MAIL_API_URL

logger = logging.getLogger("zdrovena.month_closing.zoho_mail")

HASH_FILE = ".file_hashes.json"


def _load_hashes(directory: Path) -> dict[str, str]:
    path = directory / HASH_FILE
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_hashes(directory: Path, hashes: dict[str, str]) -> None:
    path = directory / HASH_FILE
    path.write_text(json.dumps(hashes, indent=2, ensure_ascii=False), encoding="utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    name = name.strip(". ")
    return name or "attachment"


class ZohoMailClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        api_url: str = ZOHO_MAIL_API_URL,
        accounts_url: str = ZOHO_ACCOUNTS_URL,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.api_url = api_url.rstrip("/")
        self.accounts_url = accounts_url
        self.access_token: str | None = None
        self.account_id: str | None = None
        self._session = requests.Session()

    def authenticate(self) -> None:
        logger.info("Refreshing Zoho OAuth access token …")
        resp = self._session.post(
            self.accounts_url,
            params={
                "refresh_token": self.refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "refresh_token",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if "access_token" not in data:
            raise RuntimeError(f"OAuth token refresh failed: {data}")
        self.access_token = data["access_token"]
        logger.info("Access token obtained (expires in %s s)", data.get("expires_in"))
        self._fetch_account_id()

    def _fetch_account_id(self) -> None:
        data = self._api_get("/accounts")
        accounts = data.get("data", [])
        if not accounts:
            raise RuntimeError("No Zoho Mail accounts found.")
        self.account_id = str(accounts[0]["accountId"])

    def _auth_headers(self) -> dict[str, str]:
        if not self.access_token:
            raise RuntimeError("Not authenticated.")
        return {"Authorization": f"Zoho-oauthtoken {self.access_token}"}

    def _api_get(self, endpoint: str, params: dict | None = None) -> dict:
        url = f"{self.api_url}{endpoint}"
        resp = self._session.get(url, headers=self._auth_headers(), params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _api_get_binary(self, endpoint: str) -> bytes:
        url = f"{self.api_url}{endpoint}"
        resp = self._session.get(url, headers=self._auth_headers(), timeout=30)
        resp.raise_for_status()
        return resp.content

    def search_and_download_vendor(
        self,
        vendor_name: str,
        search_term: str,
        date_from: str,
        date_to: str,
        save_dir: Path,
        dry_run: bool = False,
        link_re: str | None = None,
        manual: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        is_link_vendor = bool(link_re)
        skip_attachment = is_link_vendor or manual
        try:
            messages = self._search_vendor(
                date_from, date_to, search_term, require_attachment=not skip_attachment
            )
        except Exception as exc:
            logger.error("Zoho search failed for %s: %s", vendor_name, exc)
            return {"found": False, "downloaded": 0, "manual_note": None}
        if not messages:
            return {"found": False, "downloaded": 0, "manual_note": None}

        if manual:
            invoice_url_re = kwargs.get("invoice_url_re")
            details = self._extract_manual_invoice_details(messages, invoice_url_re=invoice_url_re)
            urls = [d["url"] for d in details if d.get("url")]
            summaries = [d["summary"] for d in details]
            note = (
                f"Download {vendor_name} invoice(s) manually: {'; '.join(summaries)}"
                if summaries
                else f"Download {vendor_name} invoice(s) manually ({len(messages)} email(s) found)"
            )
            return {"found": True, "downloaded": 0, "manual_note": note, "urls": urls}

        if dry_run:
            return {"found": True, "downloaded": len(messages), "manual_note": None}

        save_dir.mkdir(parents=True, exist_ok=True)
        hashes = _load_hashes(save_dir)
        all_saved_paths: list[Path] = []
        seen_urls: set[str] = set()
        seen_invoice_ids: set[str] = set()

        for msg in messages:
            try:
                if is_link_vendor:
                    saved, _dups = self._download_from_link(
                        msg,
                        save_dir,
                        hashes,
                        vendor_prefix=vendor_name,
                        link_pattern=link_re,
                        seen_urls=seen_urls,
                        seen_invoice_ids=seen_invoice_ids,
                    )
                else:
                    saved, _dups = self._download_attachments(
                        msg, save_dir, hashes, vendor_prefix=vendor_name
                    )
                all_saved_paths.extend(saved)
            except Exception as exc:
                logger.error("Failed to process email from %s: %s", vendor_name, exc)

        _save_hashes(save_dir, hashes)
        return {
            "found": True,
            "downloaded": len(all_saved_paths),
            "manual_note": None,
            "saved_paths": all_saved_paths,
        }

    def extract_invoice_ids(
        self, search_term: str, date_from: str, date_to: str, invoice_id_re: str
    ) -> list[dict[str, str]]:
        try:
            messages = self._search_vendor(
                date_from, date_to, search_term, require_attachment=False
            )
        except Exception:
            return []
        if not messages:
            return []
        results: list[dict[str, str]] = []
        for msg in messages:
            msg_id = msg.get("messageId")
            folder_id = msg.get("folderId")
            if not msg_id or not folder_id:
                continue
            try:
                detail = self._api_get(
                    f"/accounts/{self.account_id}/folders/{folder_id}/messages/{msg_id}/content"
                )
            except Exception:
                continue
            content = ""
            data = detail.get("data", {})
            if isinstance(data, dict):
                content = data.get("content", "")
            elif isinstance(data, str):
                content = data
            if not content:
                continue
            id_matches = re.findall(invoice_id_re, content)
            if not id_matches:
                continue
            for inv_id in dict.fromkeys(id_matches):
                url_match = re.search(
                    rf'href=["\']([^"\']*{re.escape(inv_id)}[^"\']*)["\']', content
                )
                url = url_match.group(1) if url_match else ""
                results.append({"id": str(inv_id), "url": url})
        seen: set[str] = set()
        unique: list[dict[str, str]] = []
        for r in results:
            if r["id"] not in seen:
                seen.add(r["id"])
                unique.append(r)
        return unique

    # ── Private helpers ──────────────────────────────────────────────────────

    def _search_vendor(
        self,
        date_from: str,
        date_to: str,
        from_pattern: str,
        max_pages: int = 5,
        require_attachment: bool = True,
    ) -> list[dict]:
        from datetime import datetime

        try:
            dt_from = datetime.strptime(date_from, "%Y/%m/%d").replace(tzinfo=UTC)
            dt_to = datetime.strptime(date_to, "%Y/%m/%d").replace(
                hour=23, minute=59, second=59, tzinfo=UTC
            )
            ts_from = int(dt_from.timestamp() * 1000)
            ts_to = int(dt_to.timestamp() * 1000)
        except ValueError:
            ts_from = 0
            ts_to = 9999999999999

        search_key = f"entire:{from_pattern}"
        matched: list[dict] = []
        start = 0
        limit = 100
        pattern_lower = from_pattern.lower()

        for _page in range(max_pages):
            try:
                data = self._api_get(
                    f"/accounts/{self.account_id}/messages/search",
                    params={"searchKey": search_key, "limit": str(limit), "start": str(start)},
                )
            except requests.HTTPError as exc:
                if exc.response is not None and exc.response.status_code == 404:
                    break
                raise

            json_status = data.get("status", {})
            if isinstance(json_status, dict) and json_status.get("code", 200) >= 400:
                break
            messages = data.get("data", [])
            if not isinstance(messages, list) or not messages:
                break

            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                sender = (msg.get("fromAddress") or "").lower()
                sender_name = (msg.get("sender") or "").lower()
                recv_ts = int(msg.get("receivedTime", 0))
                if pattern_lower not in sender and pattern_lower not in sender_name:
                    continue
                if not (ts_from <= recv_ts <= ts_to):
                    continue
                if require_attachment:
                    has_att = str(msg.get("hasAttachment", "0")) in ("1", "True", "true")
                    if not has_att:
                        continue
                matched.append(msg)

            if len(messages) < limit:
                break
            start += limit

        return matched

    def _download_attachments(
        self, message: dict, save_dir: Path, hashes: dict[str, str], vendor_prefix: str
    ) -> tuple[list[Path], int]:
        msg_id = message.get("messageId")
        folder_id = message.get("folderId")
        if not msg_id or not folder_id:
            return [], 0
        base = f"/accounts/{self.account_id}/folders/{folder_id}/messages/{msg_id}"

        attachments: list[dict] = []
        try:
            detail = self._api_get(base)
            msg_data = detail.get("data", {})
            attachments = msg_data.get("attachments", [])
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                try:
                    att_resp = self._api_get(f"{base}/attachmentinfo")
                    att_data = att_resp.get("data", {})
                    if isinstance(att_data, dict):
                        attachments = att_data.get("attachments", [])
                    elif isinstance(att_data, list):
                        attachments = att_data
                except requests.HTTPError:
                    return [], 0
            else:
                raise

        return self._save_pdf_attachments(attachments, base, save_dir, hashes, vendor_prefix)

    def _save_pdf_attachments(
        self,
        attachments: list[dict],
        base_url: str,
        save_dir: Path,
        hashes: dict[str, str],
        vendor_prefix: str,
    ) -> tuple[list[Path], int]:
        saved: list[Path] = []
        duplicates = 0
        for att in attachments:
            att_name = att.get("attachmentName", "")
            att_id = att.get("attachmentId")
            if not att_name.lower().endswith(".pdf") or not att_id:
                continue
            filename = _safe_filename(att_name)
            if not filename.lower().endswith(".pdf"):
                filename += ".pdf"
            target = save_dir / filename
            if target.exists():
                duplicates += 1
                continue
            content = self._api_get_binary(f"{base_url}/attachments/{att_id}")
            if not content:
                continue
            file_hash = _sha256(content)
            if file_hash in hashes:
                duplicates += 1
                continue
            target.write_bytes(content)
            hashes[file_hash] = target.name
            saved.append(target)
        return saved, duplicates

    def _download_from_link(
        self,
        message: dict,
        save_dir: Path,
        hashes: dict[str, str],
        vendor_prefix: str,
        link_pattern: str,
        seen_urls: set[str] | None = None,
        seen_invoice_ids: set[str] | None = None,
    ) -> tuple[list[Path], int]:
        msg_id = message.get("messageId")
        folder_id = message.get("folderId")
        if not msg_id or not folder_id:
            return [], 0
        try:
            detail = self._api_get(
                f"/accounts/{self.account_id}/folders/{folder_id}/messages/{msg_id}/content"
            )
        except Exception as exc:
            logger.error("  Failed to get message content: %s", exc)
            return [], 0

        content = ""
        data = detail.get("data", {})
        if isinstance(data, dict):
            content = data.get("content", "")
        elif isinstance(data, str):
            content = data
        if not content:
            return [], 0

        urls = re.findall(link_pattern, content)
        if not urls:
            return [], 0

        unique_urls = list(dict.fromkeys(urls))
        if seen_urls is not None:
            unique_urls = [u for u in unique_urls if u not in seen_urls]
            if not unique_urls:
                return [], 0

        saved: list[Path] = []
        duplicates = 0

        for url in unique_urls:
            try:
                filename = self._filename_from_link(url, content, vendor_prefix)
                inv_id = Path(filename).stem
                if inv_id.startswith(vendor_prefix + "_"):
                    inv_id = inv_id[len(vendor_prefix) + 1 :]

                if seen_invoice_ids is not None and inv_id in seen_invoice_ids:
                    duplicates += 1
                    if seen_urls is not None:
                        seen_urls.add(url)
                    continue

                target = save_dir / filename
                if target.exists():
                    duplicates += 1
                    if seen_invoice_ids is not None:
                        seen_invoice_ids.add(inv_id)
                    if seen_urls is not None:
                        seen_urls.add(url)
                    continue

                resp = self._session.get(url, timeout=30)
                resp.raise_for_status()
                pdf_data = resp.content
                if not pdf_data or not pdf_data[:5].startswith(b"%PDF"):
                    continue

                file_hash = _sha256(pdf_data)
                if file_hash in hashes:
                    duplicates += 1
                    if seen_urls is not None:
                        seen_urls.add(url)
                    if seen_invoice_ids is not None:
                        seen_invoice_ids.add(inv_id)
                    continue

                target.write_bytes(pdf_data)
                hashes[file_hash] = target.name
                saved.append(target)
                if seen_urls is not None:
                    seen_urls.add(url)
                if seen_invoice_ids is not None:
                    seen_invoice_ids.add(inv_id)
            except Exception as exc:
                logger.error("  Failed to download PDF from %s: %s", url[:80], exc)

        return saved, duplicates

    @staticmethod
    def _filename_from_link(url: str, html_content: str, vendor_prefix: str) -> str:
        url_inv = re.search(r"[?&]invoice=(\d+)", url)
        if url_inv:
            return f"{vendor_prefix}_{url_inv.group(1)}.pdf"
        plain = re.sub(r"<[^>]+>", " ", html_content)
        plain = re.sub(r"&[a-z]+;", " ", plain)
        plain = re.sub(r"\s+", " ", plain)
        inv_match = re.search(
            r"(?:Numer faktury|Nr faktury|Invoice)\s*[:#]?\s*(\S+)", plain, re.IGNORECASE
        )
        if inv_match:
            inv_num = _safe_filename(inv_match.group(1).strip())
            return f"{vendor_prefix}_{inv_num}.pdf"
        return f"{vendor_prefix}_invoice.pdf"

    def _extract_manual_invoice_details(
        self, messages: list[dict], invoice_url_re: str | None = None
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            msg_id = msg.get("messageId")
            folder_id = msg.get("folderId")
            if not msg_id or not folder_id:
                continue
            try:
                detail = self._api_get(
                    f"/accounts/{self.account_id}/folders/{folder_id}/messages/{msg_id}/content"
                )
            except Exception:
                continue
            content = ""
            data = detail.get("data", {})
            if isinstance(data, dict):
                content = data.get("content", "")
            elif isinstance(data, str):
                content = data
            if not content:
                continue
            subject = str(msg.get("subject", ""))
            subject_lower = subject.lower()
            if not any(
                kw in subject_lower or kw in content.lower()
                for kw in ("invoice", "faktura", "rachunek", "payment", "charged")
            ):
                continue

            url = None
            if invoice_url_re:
                url_matches = re.findall(invoice_url_re, content)
                if url_matches:
                    url = url_matches[0]

            text = re.sub(r"<style[^>]*>.*?</style>", "", content, flags=re.DOTALL)
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"&[a-z]+;", " ", text)
            text = re.sub(r"\s+", " ", text).strip()

            inv_num = re.search(r"(?:Invoice|Faktura|INVOICE)[:\s#]*(\S+)", text)
            charged = re.search(r"(?:Charged|Total|Suma)[:\s]*(?:PLN|EUR|USD)\s*([\d,.]+)", text)
            if not charged:
                charged = re.search(r"(?:PLN|EUR|USD)\s*([\d,.]+)", text)
            issue_date = re.search(
                r"(?:Date of Issue|Data wystawienia)[:\s]*([A-Za-z]+ \d+, \d{4}|\d{4}-\d{2}-\d{2})",
                text,
            )

            parts: list[str] = []
            if inv_num:
                parts.append(f"#{inv_num.group(1)}")
            if charged:
                parts.append(f"PLN {charged.group(1)}")
            if issue_date:
                parts.append(f"({issue_date.group(1)})")
            summary = " ".join(parts) if parts else subject
            results.append({"summary": summary, "url": url, "subject": subject})
        return results
