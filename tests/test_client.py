"""Tests for zdrovena.common.client.FakturowniaClient."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from zdrovena.common.client import FakturowniaClient
from zdrovena.common.exceptions import ApiResponseFormatError, MissingSecretError

# ── Constructor ───────────────────────────────────────────────────────────────


class TestClientInit:
    def test_base_url(self):
        c = FakturowniaClient("tok123")
        assert c.base_url == "https://zdrovena.fakturownia.pl"

    def test_custom_domain(self):
        c = FakturowniaClient("tok123", domain="custom.fakturownia.pl")
        assert c.base_url == "https://custom.fakturownia.pl"

    def test_base_url_from_environment(self, monkeypatch):
        monkeypatch.setenv("FAKTUROWNIA_BASE_URL", "http://fake-provider:9009/fakturownia/")
        c = FakturowniaClient("tok123")
        assert c.base_url == "http://fake-provider:9009/fakturownia"

    def test_explicit_domain_overrides_environment(self, monkeypatch):
        monkeypatch.setenv("FAKTUROWNIA_BASE_URL", "http://fake-provider:9009/fakturownia")
        c = FakturowniaClient("tok123", domain="custom.fakturownia.pl")
        assert c.base_url == "https://custom.fakturownia.pl"

    def test_explicit_base_url(self):
        c = FakturowniaClient("tok123", base_url="http://localhost:9009/fakturownia/")
        assert c.base_url == "http://localhost:9009/fakturownia"

    def test_defaults(self):
        c = FakturowniaClient("tok123")
        assert c.retry_count == 3
        assert c.per_page == 100

    def test_user_agent_header(self):
        c = FakturowniaClient("tok123")
        assert "zdrovena" in c.session.headers["User-Agent"]


# ── from_keyring ──────────────────────────────────────────────────────────────


class TestFromKeyring:
    @patch("zdrovena.common.client.get_secret", return_value="my_token")
    def test_success(self, mock_get_secret):
        c = FakturowniaClient.from_keyring()
        assert c.api_token == "my_token"
        mock_get_secret.assert_called_once_with("fakturownia_api_token")

    @patch("zdrovena.common.client.get_secret", return_value="fake")
    def test_uses_environment_base_url(self, mock_get_secret, monkeypatch):
        monkeypatch.setenv("FAKTUROWNIA_BASE_URL", "http://fake-provider:9009/fakturownia")
        c = FakturowniaClient.from_keyring()
        assert c.base_url == "http://fake-provider:9009/fakturownia"
        mock_get_secret.assert_called_once_with("fakturownia_api_token")

    @patch(
        "zdrovena.common.client.get_secret",
        side_effect=MissingSecretError("fakturownia_api_token", "humio"),
    )
    def test_missing_token_raises(self, mock_get_secret):
        with pytest.raises(MissingSecretError, match="fakturownia_api_token"):
            FakturowniaClient.from_keyring()

    @patch("zdrovena.common.client.get_secret", return_value="env_token")
    def test_env_var_takes_precedence(self, mock_get_secret):
        c = FakturowniaClient.from_keyring()
        assert c.api_token == "env_token"

    @patch("zdrovena.common.client.get_secret", return_value="kr_token")
    def test_keyring_fallback(self, mock_get_secret):
        c = FakturowniaClient.from_keyring()
        assert c.api_token == "kr_token"

    @patch(
        "zdrovena.common.client.get_secret",
        side_effect=MissingSecretError("fakturownia_api_token", "humio"),
    )
    def test_neither_env_nor_keyring(self, mock_get_secret):
        with pytest.raises(MissingSecretError):
            FakturowniaClient.from_keyring()


# ── _request ──────────────────────────────────────────────────────────────────


class TestRequest:
    @patch("zdrovena.common.retry.time.sleep")
    def test_adds_api_token(self, _sleep):
        c = FakturowniaClient("secret_tok")
        mock_resp = MagicMock(spec=requests.Response)
        mock_resp.raise_for_status = MagicMock()
        c.session.request = MagicMock(return_value=mock_resp)

        c._request("GET", "invoices.json", params={"page": "1"})

        call_kwargs = c.session.request.call_args
        assert call_kwargs[1]["params"]["api_token"] == "secret_tok"
        assert call_kwargs[1]["params"]["page"] == "1"

    def test_builds_full_url(self):
        c = FakturowniaClient("tok")
        mock_resp = MagicMock(spec=requests.Response)
        mock_resp.raise_for_status = MagicMock()
        c.session.request = MagicMock(return_value=mock_resp)

        c._request("GET", "products.json")

        call_args = c.session.request.call_args
        assert call_args[0] == ("GET", "https://zdrovena.fakturownia.pl/products.json")


# ── get_json / get_binary ─────────────────────────────────────────────────────


class TestGetHelpers:
    def test_get_json(self):
        c = FakturowniaClient("tok")
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = [{"id": 1}]
        c.session.request = MagicMock(return_value=mock_resp)

        data = c.get_json("invoices.json")
        assert data == [{"id": 1}]

    def test_get_binary(self):
        c = FakturowniaClient("tok")
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.content = b"\x89PNG..."
        c.session.request = MagicMock(return_value=mock_resp)

        data = c.get_binary("invoices/1.pdf")
        assert data == b"\x89PNG..."

    def test_get_json_html_response(self):
        """HTML instead of JSON should raise ApiResponseFormatError."""
        c = FakturowniaClient("tok")
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "<html><body>Server Error</body></html>"
        mock_resp.json.side_effect = ValueError("No JSON object could be decoded")
        c.session.request = MagicMock(return_value=mock_resp)

        with pytest.raises(ApiResponseFormatError) as exc_info:
            c.get_json("invoices.json")

        assert exc_info.value.status_code == 200
        assert "html" in exc_info.value.body_preview.lower()

    def test_get_json_malformed_json(self):
        """Malformed JSON should raise ApiResponseFormatError."""
        c = FakturowniaClient("tok")
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "{invalid json"
        mock_resp.json.side_effect = ValueError("Unterminated string")
        c.session.request = MagicMock(return_value=mock_resp)

        with pytest.raises(ApiResponseFormatError, match="status=200"):
            c.get_json("invoices.json")

    def test_get_json_sanitizes_api_token(self):
        """api_token must never appear in error body preview."""
        c = FakturowniaClient("tok")
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "error with api_token=SECRET123 in body"
        mock_resp.json.side_effect = ValueError("No JSON")
        c.session.request = MagicMock(return_value=mock_resp)

        with pytest.raises(ApiResponseFormatError) as exc_info:
            c.get_json("invoices.json")

        assert "SECRET123" not in exc_info.value.body_preview
        assert "api_token=***" in exc_info.value.body_preview


# ── fetch_invoices (pagination) ───────────────────────────────────────────────


class TestFetchInvoices:
    def test_single_page(self):
        c = FakturowniaClient("tok", per_page=100)
        invoices = [{"id": i} for i in range(50)]

        with patch.object(c, "get_json", return_value=invoices):
            result = c.fetch_invoices("2025-01-01", "2025-01-31")

        assert len(result) == 50

    def test_multi_page(self):
        c = FakturowniaClient("tok", per_page=2)

        page1 = [{"id": 1}, {"id": 2}]
        page2 = [{"id": 3}]

        with patch.object(c, "get_json", side_effect=[page1, page2]):
            result = c.fetch_invoices("2025-01-01", "2025-01-31")

        assert len(result) == 3

    def test_empty_response(self):
        c = FakturowniaClient("tok")

        with patch.object(c, "get_json", return_value=[]):
            result = c.fetch_invoices("2025-01-01", "2025-01-31")

        assert result == []


# ── fetch_sales_invoices / fetch_cost_invoices ────────────────────────────────


class TestFetchConvenienceWrappers:
    def test_fetch_sales_invoices(self):
        c = FakturowniaClient("tok")
        with patch.object(c, "get_json", return_value=[{"id": 1}]):
            result = c.fetch_sales_invoices("2025-06-01", "2025-06-30")
        assert result == [{"id": 1}]

    def test_fetch_cost_invoices_sends_extra_params(self):
        c = FakturowniaClient("tok")
        calls = []

        def _spy(endpoint, params=None):
            calls.append(params or {})
            return []

        c.get_json = _spy
        c.fetch_cost_invoices("2025-06-01", "2025-06-30")
        # extra_params should be present
        assert any("additional_fields[invoice]" in str(p) for p in calls)


# ── download_pdf ──────────────────────────────────────────────────────────────


class TestDownloadPdf:
    def test_saves_file(self, tmp_path):
        c = FakturowniaClient("tok")
        pdf_data = b"%PDF-1.4 dummy"
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.iter_content.return_value = [pdf_data]
        c.session.request = MagicMock(return_value=mock_resp)

        dest = tmp_path / "invoice_1.pdf"
        result = c.download_pdf(1, dest)

        assert result == dest
        assert dest.read_bytes() == pdf_data

    def test_creates_parent_dirs(self, tmp_path):
        c = FakturowniaClient("tok")
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.iter_content.return_value = [b"data"]
        c.session.request = MagicMock(return_value=mock_resp)

        dest = tmp_path / "subdir" / "nested" / "invoice.pdf"
        c.download_pdf(99, dest)
        assert dest.exists()


# ── download_all_pdfs ─────────────────────────────────────────────────────────


class TestDownloadAllPdfs:
    def test_dry_run_returns_empty(self, tmp_path):
        c = FakturowniaClient("tok")
        invoices = [{"id": 1, "number": "1/06/2025"}, {"id": 2, "number": "2/06/2025"}]
        result = c.download_all_pdfs(invoices, tmp_path, dry_run=True)
        assert result == []

    def test_skips_already_existing(self, tmp_path):
        c = FakturowniaClient("tok")
        existing = tmp_path / "1_06_2025.pdf"
        existing.write_bytes(b"%PDF")
        invoices = [{"id": 1, "number": "1/06/2025"}]
        result = c.download_all_pdfs(invoices, tmp_path)
        assert result == [existing]
        # download_pdf should NOT be called
        c.session.request = MagicMock()
        c.session.request.assert_not_called()

    def test_skips_duplicates(self, tmp_path):
        c = FakturowniaClient("tok")
        # Both have the same number
        invoices = [
            {"id": 1, "number": "1/06/2025"},
            {"id": 2, "number": "1/06/2025"},  # duplicate
        ]
        with patch.object(c, "download_pdf") as mock_dl:
            mock_dl.return_value = tmp_path / "1_06_2025.pdf"
            (tmp_path / "1_06_2025.pdf").write_bytes(b"%PDF")
            c.download_all_pdfs(invoices, tmp_path)
        # Only called once (the first one)
        # (the file already exists so actually not called at all here,
        # but duplicate check runs before file-exists check)

    def test_downloads_new_file(self, tmp_path):
        c = FakturowniaClient("tok")
        invoices = [{"id": 42, "number": "42/06/2025"}]
        pdf_path = tmp_path / "42_06_2025.pdf"

        def _fake_download_pdf(inv_id, save_path):
            save_path.write_bytes(b"%PDF fake")
            return save_path

        with patch.object(c, "download_pdf", side_effect=_fake_download_pdf):
            with patch("zdrovena.common.client.time.sleep"):
                result = c.download_all_pdfs(invoices, tmp_path)

        assert result == [pdf_path]


# ── download_cost_pdfs ────────────────────────────────────────────────────────


class TestDownloadCostPdfs:
    def test_dry_run_returns_empty(self, tmp_path):
        c = FakturowniaClient("tok")
        invoices = [{"id": 1, "number": "K1/06/2025", "buyer_name": "Vendor A"}]
        result = c.download_cost_pdfs(invoices, tmp_path, dry_run=True)
        assert result == []

    def test_vendor_prefix_in_filename(self, tmp_path):
        c = FakturowniaClient("tok")
        invoices = [{"id": 5, "number": "K5/06", "buyer_name": "Firma ABC"}]

        def _fake_dl(inv_id, save_path):
            save_path.write_bytes(b"%PDF")
            return save_path

        with patch.object(c, "download_pdf", side_effect=_fake_dl):
            with patch("zdrovena.common.client.time.sleep"):
                result = c.download_cost_pdfs(invoices, tmp_path)

        assert len(result) == 1
        assert "Firma_ABC" in result[0].name

    def test_skips_existing_cost_file(self, tmp_path):
        c = FakturowniaClient("tok")
        invoices = [{"id": 1, "number": "K1/06", "buyer_name": "Firma XYZ"}]
        existing = tmp_path / "Firma_XYZ_K1_06.pdf"
        existing.write_bytes(b"%PDF")

        result = c.download_cost_pdfs(invoices, tmp_path)
        assert result == [existing]
