"""
zdrovena.month_closing.ksef – KSeF 2.0 (Krajowy System e-Faktur) Client
=========================================================================
Integration with the Polish national e-invoicing system v2.0
using X.509 certificate authentication with XAdES signatures.
"""

from __future__ import annotations

import base64
import logging
import time
from pathlib import Path
from typing import Any

import requests

from zdrovena.common.exceptions import MissingSecretError
from zdrovena.common.retry import retry_request
from zdrovena.common.secrets import get_secret
from zdrovena.month_closing.config import (
    API_RETRY_COUNT,
    API_RETRY_DELAY,
    API_TIMEOUT,
    COMPANY_NIP,
    KEYCHAIN_SERVICE_KSEF_CERT,
    KEYCHAIN_SERVICE_KSEF_KEY,
    KEYCHAIN_SERVICE_KSEF_KEY_PASS,
    KSEF_API_URL,
    KSEF_AUTH_POLL_INTERVAL,
    KSEF_AUTH_POLL_MAX,
    KSEF_ENABLED,
)

logger = logging.getLogger("zdrovena.month_closing.ksef")

try:
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec, rsa
    from lxml import etree  # type: ignore[attr-defined]
    from signxml import methods
    from signxml.xades import XAdESSigner  # type: ignore[attr-defined]

    _KSEF_DEPS_AVAILABLE = True
except ImportError:
    _KSEF_DEPS_AVAILABLE = False

NS_AUTH = "http://ksef.mf.gov.pl/auth/token/2.0"


class KSeFClient:
    def __init__(self, api_url: str = KSEF_API_URL) -> None:
        if not _KSEF_DEPS_AVAILABLE:
            raise RuntimeError(
                "KSeF dependencies not installed. "
                "Install with: pip install zdrovena-reconciliation[ksef]"
            )
        self.api_url = api_url.rstrip("/")
        self.access_token: str | None = None
        self.refresh_token: str | None = None
        self._http = requests.Session()
        self._cert: Any = None
        self._cert_pem: bytes | None = None
        self._private_key: Any = None
        self._private_key_pem: bytes | None = None

    def _load_credentials(self) -> None:
        cert_pem = self._load_secret_bytes(KEYCHAIN_SERVICE_KSEF_CERT, "certificate")
        key_pem = self._load_secret_bytes(KEYCHAIN_SERVICE_KSEF_KEY, "private key")
        if cert_pem is None or key_pem is None:
            raise MissingSecretError(KEYCHAIN_SERVICE_KSEF_CERT)
        key_password: bytes | None = None
        key_pass_str = get_secret(KEYCHAIN_SERVICE_KSEF_KEY_PASS, required=False)
        if key_pass_str:
            key_password = key_pass_str.encode("utf-8")
        self._cert_pem = cert_pem
        self._cert = x509.load_pem_x509_certificate(cert_pem)
        self._private_key = serialization.load_pem_private_key(key_pem, password=key_password)
        self._private_key_pem = self._private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        logger.info(
            "KSeF certificate loaded: subject=%s, expires=%s",
            self._cert.subject.rfc4514_string(),
            self._cert.not_valid_after_utc.isoformat(),
        )

    @staticmethod
    def _load_secret_bytes(service: str, label: str) -> bytes | None:
        try:
            encoded = get_secret(service, required=False)
        except Exception as exc:
            logger.warning("Secret unavailable for %s: %s", label, exc)
            return None
        if not encoded:
            return None
        return base64.b64decode(encoded)

    def authenticate(self) -> str:
        self._load_credentials()
        logger.info("Requesting KSeF auth challenge …")
        challenge_resp = self._post_json(f"{self.api_url}/auth/challenge", json_body={})
        challenge = challenge_resp.get("challenge")
        timestamp = challenge_resp.get("timestamp")
        if not challenge or not timestamp:
            raise RuntimeError(f"Missing challenge/timestamp: {challenge_resp}")
        logger.info("Challenge received: %s (timestamp=%s)", challenge, timestamp)

        xml_doc = self._build_auth_token_request_xml(challenge)
        signed_xml = self._sign_xades(xml_doc)

        logger.info("Posting XAdES-signed auth request …")
        resp = self._http.post(
            f"{self.api_url}/auth/xades-signature",
            data=signed_xml,
            headers={"Content-Type": "application/xml", "Accept": "application/json"},
            timeout=API_TIMEOUT,
        )
        if resp.status_code not in (200, 202):
            logger.error(
                "KSeF auth/xades-signature returned %d:\n%s", resp.status_code, resp.text[:2000]
            )
            resp.raise_for_status()
        init_data = resp.json()
        ref_number = init_data.get("referenceNumber")
        operation_token = init_data.get("authenticationToken", {}).get("token")
        if not ref_number:
            raise RuntimeError(f"KSeF auth init failed – no referenceNumber: {init_data}")
        logger.info("Auth initiated (ref=%s), polling for tokens …", ref_number)
        self._poll_auth_status(ref_number, operation_token)
        return ref_number

    def _poll_auth_status(self, ref_number: str, operation_token: str | None) -> None:
        start = time.monotonic()
        headers = {"Accept": "application/json"}
        if operation_token:
            headers["Authorization"] = f"Bearer {operation_token}"
        while time.monotonic() - start < KSEF_AUTH_POLL_MAX:
            resp = self._http.get(
                f"{self.api_url}/auth/{ref_number}", headers=headers, timeout=API_TIMEOUT
            )
            resp.raise_for_status()
            data = resp.json()
            status = data.get("status", {})
            code = status.get("code", 0)
            desc = status.get("description", "")
            if code == 200:
                logger.info("KSeF auth success: %s", desc)
                self._redeem_tokens(operation_token)
                return
            if code >= 400:
                raise RuntimeError(f"KSeF auth failed (code={code}): {desc}")
            time.sleep(KSEF_AUTH_POLL_INTERVAL)
        raise RuntimeError(f"KSeF auth timed out after {KSEF_AUTH_POLL_MAX}s (ref={ref_number})")

    def _redeem_tokens(self, operation_token: str | None) -> None:
        logger.info("Redeeming KSeF tokens …")
        headers = {"Accept": "application/json"}
        if operation_token:
            headers["Authorization"] = f"Bearer {operation_token}"
        resp = self._http.post(
            f"{self.api_url}/auth/token/redeem", headers=headers, timeout=API_TIMEOUT
        )
        resp.raise_for_status()
        data = resp.json()
        self.access_token = data.get("accessToken", {}).get("token")
        self.refresh_token = data.get("refreshToken", {}).get("token")
        if not self.access_token:
            raise RuntimeError(f"Token redeem succeeded but no accessToken: {data}")
        logger.info("KSeF authenticated (accessToken + refreshToken obtained)")

    def _build_auth_token_request_xml(self, challenge: str) -> Any:
        nsmap = {None: NS_AUTH}
        root = etree.Element(f"{{{NS_AUTH}}}AuthTokenRequest", nsmap=nsmap)
        etree.SubElement(root, f"{{{NS_AUTH}}}Challenge").text = challenge
        ctx = etree.SubElement(root, f"{{{NS_AUTH}}}ContextIdentifier")
        etree.SubElement(ctx, f"{{{NS_AUTH}}}Nip").text = COMPANY_NIP
        etree.SubElement(root, f"{{{NS_AUTH}}}SubjectIdentifierType").text = "certificateSubject"
        return root

    def _sign_xades(self, xml_doc: Any) -> bytes:
        if self._cert_pem is None or self._private_key is None:
            raise RuntimeError("_sign_xades called before _load_credentials")
        if isinstance(self._private_key, ec.EllipticCurvePrivateKey):
            sig_algo = "ecdsa-sha256"
        elif isinstance(self._private_key, rsa.RSAPrivateKey):
            sig_algo = "rsa-sha256"
        else:
            sig_algo = "rsa-sha256"
        signer = XAdESSigner(
            method=methods.enveloped,
            signature_algorithm=sig_algo,
            digest_algorithm="sha256",
            c14n_algorithm="http://www.w3.org/2006/12/xml-c14n11",
        )
        signed_root = signer.sign(xml_doc, key=self._private_key, cert=self._cert_pem)  # type: ignore[call-arg]
        signed_xml = etree.tostring(signed_root, xml_declaration=True, encoding="utf-8")
        return signed_xml

    def query_purchase_invoices(self, date_from: str, date_to: str) -> list[dict[str, Any]]:
        url = f"{self.api_url}/invoices/query/metadata"
        headers = self._auth_headers()
        payload = {
            "subjectType": "Subject2",
            "dateRange": {
                "dateType": "Issue",
                "from": f"{date_from}T00:00:00.000+00:00",
                "to": f"{date_to}T23:59:59.999+00:00",
            },
        }
        all_invoices: list[dict[str, Any]] = []
        page_offset = 0
        page_size = 100
        while True:
            resp = self._http.post(
                url,
                json=payload,
                headers=headers,
                params={"pageOffset": page_offset, "pageSize": page_size},
                timeout=API_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            invoices = data.get("invoices", [])
            all_invoices.extend(invoices)
            if not data.get("hasMore", False):
                break
            page_offset += 1
        logger.info("KSeF returned %d purchase invoice(s)", len(all_invoices))
        return all_invoices

    def download_invoice_xml(self, ksef_number: str, save_dir: Path) -> Path | None:
        url = f"{self.api_url}/invoices/ksef/{ksef_number}"
        headers = self._auth_headers()
        headers["Accept"] = "application/xml"
        try:
            resp = self._http.get(url, headers=headers, timeout=API_TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.error("Failed to download KSeF invoice %s: %s", ksef_number, exc)
            return None
        save_dir.mkdir(parents=True, exist_ok=True)
        safe_ref = ksef_number.replace("/", "_").replace("-", "_")
        xml_path = save_dir / f"KSeF_{safe_ref}.xml"
        xml_path.write_bytes(resp.content)
        return xml_path

    def fetch_and_save_purchase_invoices(
        self, date_from: str, date_to: str, save_dir: Path, dry_run: bool = False
    ) -> dict[str, Any]:
        result: dict[str, Any] = {"count": 0, "saved_files": [], "errors": []}
        if not KSEF_ENABLED:
            return result
        try:
            self.authenticate()
        except Exception as exc:
            result["errors"].append(f"KSeF authentication failed: {exc}")
            return result
        try:
            invoices = self.query_purchase_invoices(date_from, date_to)
            result["count"] = len(invoices)
            if dry_run:
                return result
            for inv in invoices:
                ksef_num = inv.get("ksefNumber", "")
                if not ksef_num:
                    continue
                path = self.download_invoice_xml(ksef_num, save_dir)
                if path:
                    result["saved_files"].append(path)
        except Exception as exc:
            result["errors"].append(f"KSeF invoice fetch failed: {exc}")
        return result

    def _auth_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        return headers

    def _post_json(self, url: str, json_body: dict) -> dict:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        resp = self._post_with_retry(url, json_payload=json_body, headers=headers)
        try:
            return resp.json()
        except Exception:
            logger.error("KSeF returned non-JSON:\n%s", resp.text[:1000])
            raise

    def _post_with_retry(
        self,
        url: str,
        json_payload: dict | None = None,
        headers: dict[str, str] | None = None,
        data: bytes | None = None,
    ) -> requests.Response:
        return retry_request(
            self._http,
            "POST",
            url,
            max_retries=API_RETRY_COUNT,
            initial_delay=API_RETRY_DELAY,
            timeout=API_TIMEOUT,
            caller="KSeF",
            json=json_payload,
            headers=headers,
            data=data,
        )
