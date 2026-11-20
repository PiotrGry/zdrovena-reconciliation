"""Coverage for zdrovena/api/client.py — issue #550.

Complements tests/test_cli_api_client.py (which mocks ``httpx.Client`` with
MagicMock).  Here a real ``httpx.Client`` is used with ``httpx.MockTransport``
injected, so requests are actually built and serialised by httpx (headers,
query params, JSON/bytes bodies) without any network access.
"""

from __future__ import annotations

import json

import httpx
import pytest

import zdrovena.api.client as client_mod
from zdrovena.api.client import ApiClient, ApiError

BASE = "http://api.example.com"


@pytest.fixture
def transport(monkeypatch):
    """Route every httpx.Client created by ApiClient through a MockTransport.

    Returns a callable ``install(handler)`` and a list of captured requests.
    """
    captured: list[httpx.Request] = []
    real_client = httpx.Client

    def install(handler):
        def recording(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return handler(request)

        mock = httpx.MockTransport(recording)

        def factory(*args, **kwargs):
            kwargs["transport"] = mock
            return real_client(*args, **kwargs)

        monkeypatch.setattr(client_mod.httpx, "Client", factory)
        return captured

    return install


def _status(code: int):
    return lambda request: httpx.Response(code, json={"detail": "boom"})


def _connect_error(request: httpx.Request):
    raise httpx.ConnectError("Connection refused", request=request)


# ── constructor / auth ───────────────────────────────────────────────────────


class TestAuthHeaders:
    def test_token_sent_as_bearer(self, transport):
        reqs = transport(lambda r: httpx.Response(200, json={"status": "ok"}))
        ApiClient(BASE, token="jwt-123").health()
        assert reqs[0].headers["authorization"] == "Bearer jwt-123"

    @pytest.mark.parametrize("token", [None, ""])
    def test_no_or_empty_token_omits_header(self, transport, token):
        reqs = transport(lambda r: httpx.Response(200, json={}))
        ApiClient(BASE, token=token).health()
        assert "authorization" not in reqs[0].headers

    def test_base_url_used_for_requests(self, transport):
        reqs = transport(lambda r: httpx.Response(200, json={}))
        ApiClient("http://other.host:8080/api").health()
        assert str(reqs[0].url) == "http://other.host:8080/api/health"


# ── close() ──────────────────────────────────────────────────────────────────


class TestClose:
    def test_default_payload(self, transport):
        reqs = transport(lambda r: httpx.Response(200, json={"ok": True}))
        result = ApiClient(BASE).close()
        assert result == {"ok": True}
        assert reqs[0].method == "POST"
        assert reqs[0].url.path == "/close"
        assert json.loads(reqs[0].content) == {
            "year": 0,
            "month": 0,
            "dry_run": False,
            "ignore_warnings": False,
            "ignore_vendors": [],
        }

    def test_full_payload(self, transport):
        reqs = transport(lambda r: httpx.Response(200, json={}))
        ApiClient(BASE).close(
            2025, 6, dry_run=True, ignore_warnings=True, ignore_vendors=["A", "B"]
        )
        assert json.loads(reqs[0].content) == {
            "year": 2025,
            "month": 6,
            "dry_run": True,
            "ignore_warnings": True,
            "ignore_vendors": ["A", "B"],
        }

    @pytest.mark.parametrize("code", [400, 401, 403, 422, 500, 503])
    def test_http_error_wrapped(self, transport, code):
        transport(_status(code))
        with pytest.raises(ApiError, match=str(code)) as excinfo:
            ApiClient(BASE).close(2025, 6)
        assert isinstance(excinfo.value.__cause__, httpx.HTTPStatusError)

    def test_connect_error_wrapped(self, transport):
        transport(_connect_error)
        with pytest.raises(ApiError, match="Connection refused") as excinfo:
            ApiClient(BASE).close(2025, 6)
        assert isinstance(excinfo.value.__cause__, httpx.ConnectError)

    def test_single_request_no_retry_on_error(self, transport):
        reqs = transport(_status(503))
        with pytest.raises(ApiError):
            ApiClient(BASE).close(2025, 6)
        assert len(reqs) == 1


# ── list_files() ─────────────────────────────────────────────────────────────


class TestListFiles:
    def test_success_without_prefix_sends_no_query(self, transport):
        files = [{"key": "a.pdf", "size": 1}]
        reqs = transport(lambda r: httpx.Response(200, json=files))
        assert ApiClient(BASE).list_files() == files
        assert reqs[0].method == "GET"
        assert reqs[0].url.path == "/files"
        assert reqs[0].url.query == b""

    def test_prefix_sent_as_query_param(self, transport):
        reqs = transport(lambda r: httpx.Response(200, json=[]))
        ApiClient(BASE).list_files(prefix="invoices/sales/2025")
        assert reqs[0].url.params["prefix"] == "invoices/sales/2025"

    @pytest.mark.parametrize("code", [401, 404, 500])
    def test_http_error_wrapped(self, transport, code):
        transport(_status(code))
        with pytest.raises(ApiError, match=str(code)) as excinfo:
            ApiClient(BASE).list_files()
        assert isinstance(excinfo.value.__cause__, httpx.HTTPStatusError)

    def test_connect_error_wrapped(self, transport):
        transport(_connect_error)
        with pytest.raises(ApiError) as excinfo:
            ApiClient(BASE).list_files(prefix="x")
        assert isinstance(excinfo.value.__cause__, httpx.ConnectError)


# ── stream_file() ────────────────────────────────────────────────────────────


class TestStreamFile:
    def test_yields_body_bytes(self, transport):
        reqs = transport(lambda r: httpx.Response(200, content=b"%PDF-1.4 data"))
        data = b"".join(ApiClient(BASE).stream_file("invoices/2025/06/inv.pdf"))
        assert data == b"%PDF-1.4 data"
        assert reqs[0].url.path == "/files/invoices/2025/06/inv.pdf"

    def test_is_lazy_until_iterated(self, transport):
        reqs = transport(lambda r: httpx.Response(200, content=b"x"))
        gen = ApiClient(BASE).stream_file("k")
        assert reqs == []
        list(gen)
        assert len(reqs) == 1

    def test_http_error_wrapped(self, transport):
        transport(_status(404))
        with pytest.raises(ApiError, match="404"):
            list(ApiClient(BASE).stream_file("missing.pdf"))

    def test_connect_error_wrapped(self, transport):
        transport(_connect_error)
        with pytest.raises(ApiError) as excinfo:
            list(ApiClient(BASE).stream_file("k"))
        assert isinstance(excinfo.value.__cause__, httpx.ConnectError)


# ── upload_file() ────────────────────────────────────────────────────────────


class TestUploadFile:
    def test_put_with_default_content_type(self, transport):
        reqs = transport(lambda r: httpx.Response(204))
        assert ApiClient(BASE, token="t").upload_file("a/b.bin", b"\x00\x01") is None
        req = reqs[0]
        assert req.method == "PUT"
        assert req.url.path == "/files/a/b.bin"
        assert req.content == b"\x00\x01"
        assert req.headers["content-type"] == "application/octet-stream"
        assert req.headers["authorization"] == "Bearer t"

    def test_custom_content_type(self, transport):
        reqs = transport(lambda r: httpx.Response(201))
        ApiClient(BASE).upload_file("inv.pdf", b"%PDF", content_type="application/pdf")
        assert reqs[0].headers["content-type"] == "application/pdf"

    @pytest.mark.parametrize("code", [400, 409, 413, 500])
    def test_http_error_wrapped(self, transport, code):
        transport(_status(code))
        with pytest.raises(ApiError, match=str(code)) as excinfo:
            ApiClient(BASE).upload_file("k", b"x")
        assert isinstance(excinfo.value.__cause__, httpx.HTTPStatusError)

    def test_connect_error_wrapped(self, transport):
        transport(_connect_error)
        with pytest.raises(ApiError) as excinfo:
            ApiClient(BASE).upload_file("k", b"x")
        assert isinstance(excinfo.value.__cause__, httpx.ConnectError)


# ── health() ─────────────────────────────────────────────────────────────────


class TestHealth:
    def test_success(self, transport):
        reqs = transport(lambda r: httpx.Response(200, json={"status": "ok"}))
        assert ApiClient(BASE).health() == {"status": "ok"}
        assert reqs[0].method == "GET"
        assert reqs[0].url.path == "/health"

    def test_http_error_wrapped(self, transport):
        transport(_status(503))
        with pytest.raises(ApiError, match="503") as excinfo:
            ApiClient(BASE).health()
        assert isinstance(excinfo.value.__cause__, httpx.HTTPStatusError)

    def test_connect_error_wrapped(self, transport):
        transport(_connect_error)
        with pytest.raises(ApiError) as excinfo:
            ApiClient(BASE).health()
        assert isinstance(excinfo.value.__cause__, httpx.ConnectError)


def test_api_error_is_exception():
    assert issubclass(ApiError, Exception)
