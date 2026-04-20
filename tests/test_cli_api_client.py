"""TDD suite for Faza D — CLI as API client.

All tests in TestApiClient, TestCloseCommandApiMode, TestFilesCommand and
TestHealthCommand are RED until the following is implemented:

  zdrovena/api/client.py              — ApiClient + ApiError
  zdrovena/api/commands/__init__.py   — new package
  zdrovena/api/commands/files_cmd.py  — `zdrovena files list / download`
  zdrovena/api/commands/health_cmd.py — `zdrovena health`
  zdrovena/month_closing/commands/close_cmd.py  — API-mode delegation
  zdrovena/cli.py                     — register `files` and `health` subcommands

Design contract (drives implementation):

  ZDROVENA_API_URL=https://...   → CLI delegates to REST API via ApiClient
  ZDROVENA_API_TOKEN=<jwt>       → sent as "Authorization: Bearer <token>"
  Without ZDROVENA_API_URL       → close uses local orchestrator (unchanged)
                                 → files/health commands exit 1 (API-only)

TestApiCliParity is green immediately — it validates that CloseReport fields
match the CloseResponse Pydantic model (regression guard).
"""

from __future__ import annotations

import io
import os
from argparse import Namespace
from unittest.mock import MagicMock, patch

import pytest

# ─────────────────────────────────────────────────────────────────────────────
# Fixtures / shared data
# ─────────────────────────────────────────────────────────────────────────────

_CLOSE_OK = {
    "sales_invoice_count": 10,
    "sales_gross_total": "1234.56",
    "sales_pdfs_downloaded": 10,
    "cost_invoice_count": 5,
    "cost_found_vendors": {"VendorA": "ok"},
    "cost_missing_vendors": [],
    "ksef_count": 3,
    "bank_statement_found": True,
    "zip_path": "/tmp/2025-06.zip",
    "email_sent": False,
    "warnings": [],
    "errors": [],
    "steps_completed": ["step1", "step2"],
    "has_critical_errors": False,
}

_CLOSE_ERRORS = {
    **_CLOSE_OK,
    "errors": ["Missing bank statement"],
    "has_critical_errors": True,
}

_FILES = [
    {
        "key": "invoices/sales/2025/06/inv001.pdf",
        "size": 12345,
        "last_modified": "2025-06-01T10:00:00",
    },
    {
        "key": "invoices/sales/2025/06/inv002.pdf",
        "size": 9876,
        "last_modified": "2025-06-02T11:00:00",
    },
]

_HEALTH = {"status": "ok", "version": "2.0.0"}


def _close_args(**overrides) -> Namespace:
    defaults = dict(
        period="2025-06",
        period_flag=None,
        dry_run=False,
        zip=False,
        send=False,
        reset=False,
        verbose=False,
        non_interactive=False,
        ignore_warnings=False,
        ignore_vendors=[],
    )
    return Namespace(**{**defaults, **overrides})


# ─────────────────────────────────────────────────────────────────────────────
# TestApiClient  (RED until zdrovena/api/client.py exists)
# ─────────────────────────────────────────────────────────────────────────────


class TestApiClient:
    """ApiClient sends correct HTTP requests and parses responses."""

    # ── helpers ──────────────────────────────────────────────────────────────

    def _mock_http(self, post_json=None, get_json=None, iter_bytes=None, status=200):
        """Return a mock httpx session and a mock response."""
        resp = MagicMock()
        resp.status_code = status
        resp.raise_for_status.return_value = None
        resp.json.return_value = post_json or get_json or {}
        if iter_bytes is not None:
            resp.iter_bytes.return_value = iter(iter_bytes)
        mock_http = MagicMock()
        mock_http.post.return_value = resp
        mock_http.get.return_value = resp
        return mock_http, resp

    # ── close() ──────────────────────────────────────────────────────────────

    def test_close_sends_post_to_close_endpoint(self):
        from zdrovena.api.client import ApiClient

        mock_http, _ = self._mock_http(post_json=_CLOSE_OK)
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value = mock_http
            ApiClient("http://api.example.com").close(2025, 6)

        mock_http.post.assert_called_once()
        assert mock_http.post.call_args[0][0] == "/close"

    def test_close_sends_correct_payload(self):
        from zdrovena.api.client import ApiClient

        mock_http, _ = self._mock_http(post_json=_CLOSE_OK)
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value = mock_http
            ApiClient("http://api.example.com").close(
                2025, 6, dry_run=True, ignore_warnings=True, ignore_vendors=["PayU"]
            )

        payload = mock_http.post.call_args[1]["json"]
        assert payload == {
            "year": 2025,
            "month": 6,
            "dry_run": True,
            "ignore_warnings": True,
            "ignore_vendors": ["PayU"],
        }

    def test_close_includes_bearer_token_in_client_headers(self):
        from zdrovena.api.client import ApiClient

        mock_http, _ = self._mock_http(post_json=_CLOSE_OK)
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value = mock_http
            ApiClient("http://api.example.com", token="tok-123").close(2025, 6)

        init_headers = MockClient.call_args[1].get("headers", {})
        assert init_headers.get("Authorization") == "Bearer tok-123"

    def test_close_no_token_omits_auth_header(self):
        from zdrovena.api.client import ApiClient

        mock_http, _ = self._mock_http(post_json=_CLOSE_OK)
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value = mock_http
            ApiClient("http://api.example.com").close(2025, 6)

        init_headers = MockClient.call_args[1].get("headers", {})
        assert "Authorization" not in init_headers

    def test_close_base_url_passed_to_httpx_client(self):
        from zdrovena.api.client import ApiClient

        mock_http, _ = self._mock_http(post_json=_CLOSE_OK)
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value = mock_http
            ApiClient("http://custom-host:9000").close(2025, 6)

        assert MockClient.call_args[1].get("base_url") == "http://custom-host:9000"

    def test_close_returns_response_dict(self):
        from zdrovena.api.client import ApiClient

        mock_http, _ = self._mock_http(post_json=_CLOSE_OK)
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value = mock_http
            result = ApiClient("http://api.example.com").close(2025, 6)

        assert isinstance(result, dict)
        assert result["sales_invoice_count"] == 10
        assert result["errors"] == []

    def test_close_raises_api_error_on_http_status_error(self):
        import httpx

        from zdrovena.api.client import ApiClient, ApiError

        mock_http, resp = self._mock_http(status=500)
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "500", request=MagicMock(), response=resp
        )
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value = mock_http
            with pytest.raises(ApiError):
                ApiClient("http://api.example.com").close(2025, 6)

    def test_close_raises_api_error_on_connection_failure(self):
        import httpx

        from zdrovena.api.client import ApiClient, ApiError

        mock_http, _ = self._mock_http()
        mock_http.post.side_effect = httpx.ConnectError("Connection refused")
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value = mock_http
            with pytest.raises(ApiError):
                ApiClient("http://api.example.com").close(2025, 6)

    # ── list_files() ─────────────────────────────────────────────────────────

    def test_list_files_sends_get_to_files_endpoint(self):
        from zdrovena.api.client import ApiClient

        mock_http, _ = self._mock_http(get_json=_FILES)
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value = mock_http
            result = ApiClient("http://api.example.com").list_files()

        mock_http.get.assert_called_once()
        assert mock_http.get.call_args[0][0] == "/files"
        assert result == _FILES

    def test_list_files_passes_prefix_as_query_param(self):
        from zdrovena.api.client import ApiClient

        mock_http, _ = self._mock_http(get_json=[])
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value = mock_http
            ApiClient("http://api.example.com").list_files(prefix="invoices/sales/2025")

        params = mock_http.get.call_args[1].get("params", {})
        assert params.get("prefix") == "invoices/sales/2025"

    def test_list_files_empty_prefix_is_default(self):
        from zdrovena.api.client import ApiClient

        mock_http, _ = self._mock_http(get_json=[])
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value = mock_http
            ApiClient("http://api.example.com").list_files()

        # prefix="" is fine — either omitted or passed as empty string
        params = mock_http.get.call_args[1].get("params") or {}
        assert params.get("prefix", "") == ""

    # ── stream_file() ────────────────────────────────────────────────────────

    def test_stream_file_sends_get_with_key_in_path(self):
        from zdrovena.api.client import ApiClient

        mock_http, _ = self._mock_http(iter_bytes=[b"chunk1", b"chunk2"])
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value = mock_http
            chunks = list(
                ApiClient("http://api.example.com").stream_file("invoices/sales/2025/06/inv001.pdf")
            )

        url = mock_http.get.call_args[0][0]
        assert url == "/files/invoices/sales/2025/06/inv001.pdf"
        assert chunks == [b"chunk1", b"chunk2"]

    def test_stream_file_raises_api_error_when_not_found(self):
        import httpx

        from zdrovena.api.client import ApiClient, ApiError

        mock_http, resp = self._mock_http(status=404)
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "404", request=MagicMock(), response=resp
        )
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value = mock_http
            with pytest.raises(ApiError):
                list(ApiClient("http://api.example.com").stream_file("missing.pdf"))

    # ── health() ─────────────────────────────────────────────────────────────

    def test_health_sends_get_to_health_endpoint(self):
        from zdrovena.api.client import ApiClient

        mock_http, _ = self._mock_http(get_json=_HEALTH)
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value = mock_http
            result = ApiClient("http://api.example.com").health()

        mock_http.get.assert_called_once()
        assert mock_http.get.call_args[0][0] == "/health"
        assert result == _HEALTH


# ─────────────────────────────────────────────────────────────────────────────
# TestCloseCommandApiMode  (RED until close_cmd.py is modified)
# ─────────────────────────────────────────────────────────────────────────────


class TestCloseCommandApiMode:
    """close_cmd._run() delegates to ApiClient when ZDROVENA_API_URL is set."""

    def _patched_api(self, response=None, side_effect=None):
        """Context manager: patch ApiClient, return (MockClass, mock_instance)."""
        MockClass = MagicMock()
        mock_inst = MagicMock()
        MockClass.return_value = mock_inst
        if side_effect:
            mock_inst.close.side_effect = side_effect
        else:
            mock_inst.close.return_value = response or _CLOSE_OK
        return MockClass, mock_inst

    # ── routing ──────────────────────────────────────────────────────────────

    def test_delegates_to_api_when_url_set(self):
        from zdrovena.month_closing.commands import close_cmd

        MockClass, mock_inst = self._patched_api()
        with patch.dict(os.environ, {"ZDROVENA_API_URL": "http://api.example.com"}):
            with patch("zdrovena.api.client.ApiClient", MockClass):
                with pytest.raises(SystemExit) as exc:
                    close_cmd._run(_close_args())

        mock_inst.close.assert_called_once()
        assert exc.value.code == 0

    def test_local_orchestrator_used_when_url_not_set(self):
        """With no ZDROVENA_API_URL, orchestrator must be called (not ApiClient)."""
        from zdrovena.month_closing.commands import close_cmd

        MockClass, _ = self._patched_api()
        env = {k: v for k, v in os.environ.items() if k != "ZDROVENA_API_URL"}
        with patch.dict(os.environ, env, clear=True):
            with patch("zdrovena.api.client.ApiClient", MockClass):
                with patch("zdrovena.month_closing.commands.close_cmd._run_local") as mock_local:
                    close_cmd._run(_close_args())
                    mock_local.assert_called_once()

    # ── payload forwarding ───────────────────────────────────────────────────

    def test_passes_year_and_month_to_api(self):
        from zdrovena.month_closing.commands import close_cmd

        MockClass, mock_inst = self._patched_api()
        with patch.dict(os.environ, {"ZDROVENA_API_URL": "http://api.example.com"}):
            with patch("zdrovena.api.client.ApiClient", MockClass):
                with pytest.raises(SystemExit):
                    close_cmd._run(_close_args(period="2025-06"))

        kw = mock_inst.close.call_args[1]
        assert kw["year"] == 2025
        assert kw["month"] == 6

    def test_passes_dry_run_to_api(self):
        from zdrovena.month_closing.commands import close_cmd

        MockClass, mock_inst = self._patched_api()
        with patch.dict(os.environ, {"ZDROVENA_API_URL": "http://api.example.com"}):
            with patch("zdrovena.api.client.ApiClient", MockClass):
                with pytest.raises(SystemExit):
                    close_cmd._run(_close_args(dry_run=True))

        assert mock_inst.close.call_args[1].get("dry_run") is True

    def test_passes_ignore_warnings_to_api(self):
        from zdrovena.month_closing.commands import close_cmd

        MockClass, mock_inst = self._patched_api()
        with patch.dict(os.environ, {"ZDROVENA_API_URL": "http://api.example.com"}):
            with patch("zdrovena.api.client.ApiClient", MockClass):
                with pytest.raises(SystemExit):
                    close_cmd._run(_close_args(ignore_warnings=True))

        assert mock_inst.close.call_args[1].get("ignore_warnings") is True

    def test_passes_ignore_vendors_to_api(self):
        from zdrovena.month_closing.commands import close_cmd

        MockClass, mock_inst = self._patched_api()
        with patch.dict(os.environ, {"ZDROVENA_API_URL": "http://api.example.com"}):
            with patch("zdrovena.api.client.ApiClient", MockClass):
                with pytest.raises(SystemExit):
                    close_cmd._run(_close_args(ignore_vendors=["PayU", "Stripe"]))

        assert mock_inst.close.call_args[1].get("ignore_vendors") == ["PayU", "Stripe"]

    # ── exit codes ───────────────────────────────────────────────────────────

    def test_exits_0_on_success(self):
        from zdrovena.month_closing.commands import close_cmd

        MockClass, _ = self._patched_api(response=_CLOSE_OK)
        with patch.dict(os.environ, {"ZDROVENA_API_URL": "http://api.example.com"}):
            with patch("zdrovena.api.client.ApiClient", MockClass):
                with pytest.raises(SystemExit) as exc:
                    close_cmd._run(_close_args())

        assert exc.value.code == 0

    def test_exits_1_when_response_contains_errors(self):
        from zdrovena.month_closing.commands import close_cmd

        MockClass, _ = self._patched_api(response=_CLOSE_ERRORS)
        with patch.dict(os.environ, {"ZDROVENA_API_URL": "http://api.example.com"}):
            with patch("zdrovena.api.client.ApiClient", MockClass):
                with pytest.raises(SystemExit) as exc:
                    close_cmd._run(_close_args())

        assert exc.value.code == 1

    def test_exits_1_on_api_error(self):
        from zdrovena.api.client import ApiError
        from zdrovena.month_closing.commands import close_cmd

        MockClass, _ = self._patched_api(side_effect=ApiError("Connection refused"))
        with patch.dict(os.environ, {"ZDROVENA_API_URL": "http://api.example.com"}):
            with patch("zdrovena.api.client.ApiClient", MockClass):
                with pytest.raises(SystemExit) as exc:
                    close_cmd._run(_close_args())

        assert exc.value.code == 1

    # ── ApiClient construction ────────────────────────────────────────────────

    def test_api_url_passed_to_client_constructor(self):
        from zdrovena.month_closing.commands import close_cmd

        MockClass, _ = self._patched_api()
        with patch.dict(os.environ, {"ZDROVENA_API_URL": "http://custom-host:9000"}):
            with patch("zdrovena.api.client.ApiClient", MockClass):
                with pytest.raises(SystemExit):
                    close_cmd._run(_close_args())

        init_pos = MockClass.call_args[0]
        init_kw = MockClass.call_args[1]
        url = init_pos[0] if init_pos else init_kw.get("base_url", "")
        assert url == "http://custom-host:9000"

    def test_token_read_from_env_and_passed_to_client(self):
        from zdrovena.month_closing.commands import close_cmd

        MockClass, _ = self._patched_api()
        env = {
            "ZDROVENA_API_URL": "http://api.example.com",
            "ZDROVENA_API_TOKEN": "my-jwt-token",
        }
        with patch.dict(os.environ, env), patch("zdrovena.api.client.ApiClient", MockClass):
            with pytest.raises(SystemExit):
                close_cmd._run(_close_args())

        init_kw = MockClass.call_args[1]
        assert init_kw.get("token") == "my-jwt-token"

    def test_no_token_env_passes_none(self):
        from zdrovena.month_closing.commands import close_cmd

        MockClass, _ = self._patched_api()
        env = {k: v for k, v in os.environ.items() if k != "ZDROVENA_API_TOKEN"}
        env["ZDROVENA_API_URL"] = "http://api.example.com"
        with patch.dict(os.environ, env, clear=True):
            with patch("zdrovena.api.client.ApiClient", MockClass):
                with pytest.raises(SystemExit):
                    close_cmd._run(_close_args())

        init_kw = MockClass.call_args[1]
        assert init_kw.get("token") is None

    # ── output ───────────────────────────────────────────────────────────────

    def test_prints_summary_on_success(self, capsys):
        from zdrovena.month_closing.commands import close_cmd

        MockClass, _ = self._patched_api(response=_CLOSE_OK)
        with patch.dict(os.environ, {"ZDROVENA_API_URL": "http://api.example.com"}):
            with patch("zdrovena.api.client.ApiClient", MockClass):
                with pytest.raises(SystemExit):
                    close_cmd._run(_close_args())

        out = capsys.readouterr().out
        # Should print invoice count or total — something from the response
        assert "10" in out or "1234.56" in out


# ─────────────────────────────────────────────────────────────────────────────
# TestFilesCommand  (RED until zdrovena/api/commands/files_cmd.py exists)
# ─────────────────────────────────────────────────────────────────────────────


class TestFilesCommand:
    """`zdrovena files list` and `zdrovena files download` subcommands."""

    # ── CLI registration ─────────────────────────────────────────────────────

    def test_files_subcommand_in_cli_help(self):
        import subprocess
        import sys

        r = subprocess.run(
            [sys.executable, "-m", "zdrovena.cli", "--help"],
            capture_output=True,
            text=True,
        )
        assert "files" in r.stdout

    def test_files_list_help_shows_prefix_option(self):
        import subprocess
        import sys

        r = subprocess.run(
            [sys.executable, "-m", "zdrovena.cli", "files", "list", "--help"],
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0
        assert "--prefix" in r.stdout

    def test_files_download_help_shows_key_and_output(self):
        import subprocess
        import sys

        r = subprocess.run(
            [sys.executable, "-m", "zdrovena.cli", "files", "download", "--help"],
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0
        assert "key" in r.stdout.lower() or "KEY" in r.stdout

    # ── list ─────────────────────────────────────────────────────────────────

    def test_list_calls_api_list_files(self):
        from zdrovena.api.commands.files_cmd import _run_list

        MockClass = MagicMock()
        mock_inst = MagicMock()
        MockClass.return_value = mock_inst
        mock_inst.list_files.return_value = _FILES

        with patch.dict(os.environ, {"ZDROVENA_API_URL": "http://api.example.com"}):
            with patch("zdrovena.api.client.ApiClient", MockClass):
                _run_list(Namespace(prefix=""))

        mock_inst.list_files.assert_called_once()

    def test_list_prints_file_keys_to_stdout(self, capsys):
        from zdrovena.api.commands.files_cmd import _run_list

        MockClass = MagicMock()
        mock_inst = MagicMock()
        MockClass.return_value = mock_inst
        mock_inst.list_files.return_value = _FILES

        with patch.dict(os.environ, {"ZDROVENA_API_URL": "http://api.example.com"}):
            with patch("zdrovena.api.client.ApiClient", MockClass):
                _run_list(Namespace(prefix=""))

        out = capsys.readouterr().out
        assert "invoices/sales/2025/06/inv001.pdf" in out
        assert "invoices/sales/2025/06/inv002.pdf" in out

    def test_list_passes_prefix_to_api(self):
        from zdrovena.api.commands.files_cmd import _run_list

        MockClass = MagicMock()
        mock_inst = MagicMock()
        MockClass.return_value = mock_inst
        mock_inst.list_files.return_value = []

        with patch.dict(os.environ, {"ZDROVENA_API_URL": "http://api.example.com"}):
            with patch("zdrovena.api.client.ApiClient", MockClass):
                _run_list(Namespace(prefix="invoices/sales/2025"))

        kw = mock_inst.list_files.call_args[1]
        assert kw.get("prefix") == "invoices/sales/2025"

    def test_list_empty_result_does_not_crash(self, capsys):
        from zdrovena.api.commands.files_cmd import _run_list

        MockClass = MagicMock()
        mock_inst = MagicMock()
        MockClass.return_value = mock_inst
        mock_inst.list_files.return_value = []

        with patch.dict(os.environ, {"ZDROVENA_API_URL": "http://api.example.com"}):
            with patch("zdrovena.api.client.ApiClient", MockClass):
                _run_list(Namespace(prefix=""))  # must not raise

        out = capsys.readouterr().out
        assert "inv001" not in out

    def test_list_exits_1_without_api_url(self):
        from zdrovena.api.commands.files_cmd import _run_list

        env = {k: v for k, v in os.environ.items() if k != "ZDROVENA_API_URL"}
        with patch.dict(os.environ, env, clear=True), pytest.raises(SystemExit) as exc:
            _run_list(Namespace(prefix=""))
        assert exc.value.code == 1

    # ── download ─────────────────────────────────────────────────────────────

    def test_download_writes_chunks_to_output_file(self, tmp_path):
        from zdrovena.api.commands.files_cmd import _run_download

        out_file = tmp_path / "invoice.pdf"
        MockClass = MagicMock()
        mock_inst = MagicMock()
        MockClass.return_value = mock_inst
        mock_inst.stream_file.return_value = iter([b"PDF", b"content"])

        with patch.dict(os.environ, {"ZDROVENA_API_URL": "http://api.example.com"}):
            with patch("zdrovena.api.client.ApiClient", MockClass):
                _run_download(
                    Namespace(
                        key="invoices/sales/2025/06/inv001.pdf",
                        output=str(out_file),
                    )
                )

        assert out_file.read_bytes() == b"PDFcontent"

    def test_download_calls_stream_with_exact_key(self):
        from zdrovena.api.commands.files_cmd import _run_download

        MockClass = MagicMock()
        mock_inst = MagicMock()
        MockClass.return_value = mock_inst
        mock_inst.stream_file.return_value = iter([b"bytes"])

        with patch.dict(os.environ, {"ZDROVENA_API_URL": "http://api.example.com"}):
            with patch("zdrovena.api.client.ApiClient", MockClass):
                # output=None → write to stdout; supply a dummy binary buffer
                with patch("sys.stdout", new_callable=lambda: lambda: io.RawIOBase()):
                    try:
                        _run_download(
                            Namespace(
                                key="invoices/sales/2025/06/inv001.pdf",
                                output=None,
                            )
                        )
                    except Exception:
                        pass  # output plumbing may vary; what matters is stream_file call

        mock_inst.stream_file.assert_called_once_with("invoices/sales/2025/06/inv001.pdf")

    def test_download_exits_1_without_api_url(self):
        from zdrovena.api.commands.files_cmd import _run_download

        env = {k: v for k, v in os.environ.items() if k != "ZDROVENA_API_URL"}
        with patch.dict(os.environ, env, clear=True), pytest.raises(SystemExit) as exc:
            _run_download(Namespace(key="some/file.pdf", output=None))
        assert exc.value.code == 1


# ─────────────────────────────────────────────────────────────────────────────
# TestHealthCommand  (RED until zdrovena/api/commands/health_cmd.py exists)
# ─────────────────────────────────────────────────────────────────────────────


class TestHealthCommand:
    """`zdrovena health` pings the API and prints status."""

    # ── CLI registration ─────────────────────────────────────────────────────

    def test_health_subcommand_in_cli_help(self):
        import subprocess
        import sys

        r = subprocess.run(
            [sys.executable, "-m", "zdrovena.cli", "--help"],
            capture_output=True,
            text=True,
        )
        assert "health" in r.stdout

    # ── behaviour ────────────────────────────────────────────────────────────

    def test_health_calls_api_health(self):
        from zdrovena.api.commands.health_cmd import _run

        MockClass = MagicMock()
        mock_inst = MagicMock()
        MockClass.return_value = mock_inst
        mock_inst.health.return_value = _HEALTH

        with patch.dict(os.environ, {"ZDROVENA_API_URL": "http://api.example.com"}):
            with patch("zdrovena.api.client.ApiClient", MockClass):
                with pytest.raises(SystemExit) as exc:
                    _run(Namespace())

        mock_inst.health.assert_called_once()
        assert exc.value.code == 0

    def test_health_prints_ok_and_version(self, capsys):
        from zdrovena.api.commands.health_cmd import _run

        MockClass = MagicMock()
        mock_inst = MagicMock()
        MockClass.return_value = mock_inst
        mock_inst.health.return_value = _HEALTH

        with patch.dict(os.environ, {"ZDROVENA_API_URL": "http://api.example.com"}):
            with patch("zdrovena.api.client.ApiClient", MockClass):
                with pytest.raises(SystemExit):
                    _run(Namespace())

        out = capsys.readouterr().out
        assert "ok" in out
        assert "2.0.0" in out

    def test_health_exits_1_on_api_error(self):
        from zdrovena.api.client import ApiError
        from zdrovena.api.commands.health_cmd import _run

        MockClass = MagicMock()
        mock_inst = MagicMock()
        MockClass.return_value = mock_inst
        mock_inst.health.side_effect = ApiError("Connection refused")

        with patch.dict(os.environ, {"ZDROVENA_API_URL": "http://api.example.com"}):
            with patch("zdrovena.api.client.ApiClient", MockClass):
                with pytest.raises(SystemExit) as exc:
                    _run(Namespace())

        assert exc.value.code == 1

    def test_health_exits_1_without_api_url(self):
        from zdrovena.api.commands.health_cmd import _run

        env = {k: v for k, v in os.environ.items() if k != "ZDROVENA_API_URL"}
        with patch.dict(os.environ, env, clear=True), pytest.raises(SystemExit) as exc:
            _run(Namespace())
        assert exc.value.code == 1

    def test_health_prints_error_on_failure(self, capsys):
        from zdrovena.api.client import ApiError
        from zdrovena.api.commands.health_cmd import _run

        MockClass = MagicMock()
        mock_inst = MagicMock()
        MockClass.return_value = mock_inst
        mock_inst.health.side_effect = ApiError("Connection refused")

        with patch.dict(os.environ, {"ZDROVENA_API_URL": "http://api.example.com"}):
            with patch("zdrovena.api.client.ApiClient", MockClass):
                with pytest.raises(SystemExit):
                    _run(Namespace())

        combined = capsys.readouterr()
        assert "Connection refused" in combined.out or "Connection refused" in combined.err


# ─────────────────────────────────────────────────────────────────────────────
# TestApiCliParity  (GREEN — regression guard, no new code needed)
# ─────────────────────────────────────────────────────────────────────────────


class TestApiCliParity:
    """CloseReport (local dataclass) and CloseResponse (Pydantic) share same fields.

    has_critical_errors is a @property on CloseReport, so it's excluded from
    dc_fields(). We allow it to be present only in the API model.
    """

    def test_close_report_fields_subset_of_close_response(self):
        from dataclasses import fields as dc_fields

        from zdrovena.api.models import CloseResponse
        from zdrovena.month_closing.orchestrator import CloseReport

        local_fields = {f.name for f in dc_fields(CloseReport)}
        api_fields = set(CloseResponse.model_fields.keys())

        # Every local dataclass field must appear in the API response
        missing_in_api = local_fields - api_fields
        assert not missing_in_api, (
            f"Fields in CloseReport missing from CloseResponse: {missing_in_api}"
        )

    def test_close_response_only_adds_has_critical_errors(self):
        from dataclasses import fields as dc_fields

        from zdrovena.api.models import CloseResponse
        from zdrovena.month_closing.orchestrator import CloseReport

        local_fields = {f.name for f in dc_fields(CloseReport)}
        api_fields = set(CloseResponse.model_fields.keys())

        extra_in_api = api_fields - local_fields
        # The only extra field allowed is has_critical_errors (promoted from property)
        assert extra_in_api <= {"has_critical_errors"}, (
            f"Unexpected extra fields in CloseResponse: {extra_in_api - {'has_critical_errors'}}"
        )
