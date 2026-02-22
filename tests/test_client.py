"""Tests for zdrovena.common.client.FakturowniaClient."""

from __future__ import annotations

import os
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

    def test_defaults(self):
        c = FakturowniaClient("tok123")
        assert c.retry_count == 3
        assert c.per_page == 100

    def test_user_agent_header(self):
        c = FakturowniaClient("tok123")
        assert "zdrovena" in c.session.headers["User-Agent"]


# ── from_keyring ──────────────────────────────────────────────────────────────

class TestFromKeyring:
    @patch("zdrovena.common.client.keyring.get_password", return_value="my_token")
    def test_success(self, mock_keyring):
        c = FakturowniaClient.from_keyring()
        assert c.api_token == "my_token"
        mock_keyring.assert_called_once()

    @patch("zdrovena.common.client.keyring.get_password", return_value=None)
    def test_missing_token_raises(self, mock_keyring):
        with pytest.raises(MissingSecretError, match="fakturownia_api_token"):
            FakturowniaClient.from_keyring()

    @patch.dict(os.environ, {"FAKTUROWNIA_API_TOKEN": "env_token"})
    @patch("zdrovena.common.client.keyring.get_password", return_value=None)
    def test_env_var_takes_precedence(self, mock_keyring):
        c = FakturowniaClient.from_keyring()
        assert c.api_token == "env_token"

    @patch.dict(os.environ, {}, clear=True)
    @patch("zdrovena.common.client.keyring.get_password", return_value="kr_token")
    def test_keyring_fallback(self, mock_keyring):
        # Ensure env var is not set
        os.environ.pop("FAKTUROWNIA_API_TOKEN", None)
        c = FakturowniaClient.from_keyring()
        assert c.api_token == "kr_token"

    @patch.dict(os.environ, {}, clear=True)
    @patch("zdrovena.common.client.keyring.get_password", return_value=None)
    def test_neither_env_nor_keyring(self, mock_keyring):
        os.environ.pop("FAKTUROWNIA_API_TOKEN", None)
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
