"""
Tests for zdrovena.api (FastAPI layer) — health, auth, /close, /files endpoints.

All tests run with AZURE_AUTH_DISABLED=true (dev principal injected automatically).
Auth enforcement tests temporarily re-enable auth via monkeypatch.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# Ensure auth is disabled globally for the test module
os.environ.setdefault("AZURE_AUTH_DISABLED", "true")

from zdrovena.api.auth import Principal, get_current_principal
from zdrovena.api.main import app
from zdrovena.common.storage import LocalStorageService

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_client(storage):
    """Return a TestClient with the given storage injected."""
    return TestClient(app, raise_server_exceptions=True)


@pytest.fixture()
def api(tmp_path):
    """Yield (TestClient, LocalStorageService) with auth disabled."""
    storage = LocalStorageService(root=tmp_path / "storage")
    with patch("zdrovena.api.deps._storage_singleton", return_value=storage):
        with TestClient(app, raise_server_exceptions=True) as c:
            yield c, storage


def _make_report(**overrides):
    @dataclass
    class R:
        sales_invoice_count: int = 0
        sales_gross_total: Decimal = Decimal("0.00")
        sales_pdfs_downloaded: int = 0
        cost_invoice_count: int = 0
        cost_found_vendors: dict = field(default_factory=dict)
        cost_missing_vendors: list = field(default_factory=list)
        ksef_count: int = 0
        bank_statement_found: bool = False
        zip_path: Path | None = None
        email_sent: bool = False
        warnings: list = field(default_factory=list)
        errors: list = field(default_factory=list)
        steps_completed: list = field(default_factory=list)
        has_critical_errors: bool = False

    r = R()
    for k, v in overrides.items():
        setattr(r, k, v)
    return r


# ── /health ───────────────────────────────────────────────────────────────────


class TestHealth:
    def test_health_returns_ok(self, api):
        c, _ = api
        resp = c.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_health_has_version(self, api):
        c, _ = api
        assert "version" in c.get("/health").json()


# ── /files — download ─────────────────────────────────────────────────────────


class TestFilesDownload:
    def test_download_existing_file(self, api):
        c, storage = api
        (storage.root / "invoices/sales").mkdir(parents=True)
        (storage.root / "invoices/sales/faktura.pdf").write_bytes(b"PDF data")

        resp = c.get("/api/files/invoices/sales/faktura.pdf")

        assert resp.status_code == 200
        assert resp.content == b"PDF data"
        assert "attachment" in resp.headers["content-disposition"]
        assert "faktura.pdf" in resp.headers["content-disposition"]

    def test_download_missing_file_returns_404(self, api):
        c, _ = api
        resp = c.get("/api/files/missing/file.pdf")
        assert resp.status_code == 404

    def test_download_path_traversal_rejected(self, api):
        c, _ = api
        resp = c.get("/api/files/../etc/passwd")
        assert resp.status_code in (400, 404)

    def test_content_type_pdf(self, api):
        c, storage = api
        storage.root.mkdir(parents=True, exist_ok=True)
        (storage.root / "doc.pdf").write_bytes(b"%PDF")

        resp = c.get("/api/files/doc.pdf")

        assert "pdf" in resp.headers.get("content-type", "").lower()


# ── /files — list ─────────────────────────────────────────────────────────────


class TestFilesList:
    def test_list_empty(self, api):
        c, _ = api
        resp = c.get("/api/files/")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_returns_entries(self, api):
        c, storage = api
        (storage.root / "invoices").mkdir(parents=True)
        (storage.root / "invoices/a.pdf").write_bytes(b"a")
        (storage.root / "invoices/b.pdf").write_bytes(b"b")

        resp = c.get("/api/files/", params={"prefix": "invoices"})

        assert resp.status_code == 200
        keys = {f["key"] for f in resp.json()}
        assert "invoices/a.pdf" in keys
        assert "invoices/b.pdf" in keys

    def test_list_entry_shape(self, api):
        c, storage = api
        storage.root.mkdir(parents=True, exist_ok=True)
        (storage.root / "x.pdf").write_bytes(b"x")

        entry = c.get("/api/files/").json()[0]

        assert {"key", "size", "last_modified"} <= entry.keys()


# ── /files — auth enforcement ─────────────────────────────────────────────────


class TestFilesAuth:
    def test_no_token_returns_401_when_auth_enabled(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AZURE_AUTH_DISABLED", "false")
        storage = LocalStorageService(root=tmp_path / "storage")
        with patch("zdrovena.api.deps._storage_singleton", return_value=storage):
            with TestClient(app, raise_server_exceptions=True) as c:
                resp = c.get("/api/files/any/file.pdf")
        assert resp.status_code == 401

    def test_no_roles_returns_403(self, tmp_path):
        principal_no_roles = Principal(sub="u", email="u@x.com", roles=[])
        storage = LocalStorageService(root=tmp_path / "storage")
        storage.root.mkdir(parents=True, exist_ok=True)
        (storage.root / "x.pdf").write_bytes(b"x")
        app.dependency_overrides[get_current_principal] = lambda: principal_no_roles
        try:
            with patch("zdrovena.api.deps._storage_singleton", return_value=storage):
                with TestClient(app, raise_server_exceptions=False) as c:
                    resp = c.get("/api/files/x.pdf")
        finally:
            app.dependency_overrides.pop(get_current_principal, None)
        assert resp.status_code == 403


# ── /close ────────────────────────────────────────────────────────────────────


class TestCloseEndpoint:
    def test_dry_run_returns_200(self, api):
        c, _ = api
        report = _make_report(sales_invoice_count=5)
        with patch("zdrovena.api.routers.close.MonthCloseOrchestrator") as M:
            M.return_value.execute.return_value = report
            resp = c.post("/api/close", json={"year": 2026, "month": 3, "dry_run": True})

        assert resp.status_code == 200
        data = resp.json()
        assert data["sales_invoice_count"] == 5
        assert "has_critical_errors" in data

    def test_response_fields_match_report(self, api):
        c, _ = api
        report = _make_report(
            sales_invoice_count=3,
            sales_gross_total=Decimal("1500.00"),
            cost_invoice_count=7,
            warnings=["w1"],
            steps_completed=["step_1"],
        )
        with patch("zdrovena.api.routers.close.MonthCloseOrchestrator") as M:
            M.return_value.execute.return_value = report
            data = c.post("/api/close", json={"year": 2026, "month": 3}).json()

        assert data["sales_invoice_count"] == 3
        assert data["sales_gross_total"] == "1500.00"
        assert data["cost_invoice_count"] == 7
        assert data["warnings"] == ["w1"]
        assert data["steps_completed"] == ["step_1"]

    def test_invalid_month_returns_422(self, api):
        c, _ = api
        resp = c.post("/api/close", json={"year": 2026, "month": 13})
        assert resp.status_code == 422

    def test_invalid_year_returns_422(self, api):
        c, _ = api
        resp = c.post("/api/close", json={"year": 1999, "month": 3})
        assert resp.status_code == 422

    def test_orchestrator_value_error_returns_400(self, api):
        c, _ = api
        with patch(
            "zdrovena.api.routers.close.MonthCloseOrchestrator",
            side_effect=ValueError("bad"),
        ):
            resp = c.post("/api/close", json={"year": 2026, "month": 3})
        assert resp.status_code == 400

    def test_preflight_blockers_return_422_not_500(self, api):
        """SystemExit from pre-flight blockers must yield 422, never 500."""
        c, _ = api
        with patch("zdrovena.api.routers.close.MonthCloseOrchestrator") as M:
            instance = M.return_value
            instance.report.errors = ["Brakuje wyciągu bankowego"]
            instance.execute.side_effect = SystemExit(1)
            resp = c.post("/api/close", json={"year": 2026, "month": 3, "dry_run": True})
        assert resp.status_code == 422
        body = resp.json()["detail"]
        assert body["blockers"] == ["Brakuje wyciągu bankowego"]
        assert "log_lines" in body

    def test_close_requires_auth_when_enabled(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AZURE_AUTH_DISABLED", "false")
        storage = LocalStorageService(root=tmp_path / "storage")
        with patch("zdrovena.api.deps._storage_singleton", return_value=storage):
            with TestClient(app, raise_server_exceptions=True) as c:
                resp = c.post("/api/close", json={"year": 2026, "month": 3})
        assert resp.status_code == 401

    def test_close_viewer_role_returns_403(self, tmp_path):
        viewer = Principal(sub="u", email="u@x.com", roles=["zdrovena-viewer"])
        storage = LocalStorageService(root=tmp_path / "storage")
        app.dependency_overrides[get_current_principal] = lambda: viewer
        try:
            with patch("zdrovena.api.deps._storage_singleton", return_value=storage):
                with TestClient(app, raise_server_exceptions=False) as c:
                    resp = c.post("/api/close", json={"year": 2026, "month": 3})
        finally:
            app.dependency_overrides.pop(get_current_principal, None)
        assert resp.status_code == 403


# ── Principal unit tests ───────────────────────────────────────────────────────


class TestPrincipal:
    def test_has_role_match(self):
        p = Principal(sub="x", email="x@x.com", roles=["zdrovena-admin"])
        assert p.has_role("zdrovena-admin")

    def test_has_role_no_match(self):
        p = Principal(sub="x", email="x@x.com", roles=["zdrovena-viewer"])
        assert not p.has_role("zdrovena-admin")

    def test_require_role_raises_403(self):
        from fastapi import HTTPException

        p = Principal(sub="x", email="x@x.com", roles=["zdrovena-viewer"])
        with pytest.raises(HTTPException) as exc_info:
            p.require_role("zdrovena-admin")
        assert exc_info.value.status_code == 403

    def test_require_role_passes_for_correct_role(self):
        p = Principal(sub="x", email="x@x.com", roles=["zdrovena-admin"])
        p.require_role("zdrovena-admin")  # must not raise

    def test_require_role_passes_for_any_matching(self):
        p = Principal(sub="x", email="x@x.com", roles=["zdrovena-accountant"])
        p.require_role("zdrovena-admin", "zdrovena-accountant")


# ── _validate_token unit tests (PyJWT migration) ─────────────────────────────


class TestValidateToken:
    """Coverage for _validate_token — issuer, audience, JWKS errors."""

    def _make_token(self, payload: dict, key=None):
        """Build a signed RS256 JWT using a fresh RSA key."""
        import jwt
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives.asymmetric import rsa

        if key is None:
            key = rsa.generate_private_key(
                public_exponent=65537, key_size=2048, backend=default_backend()
            )
        token = jwt.encode(payload, key, algorithm="RS256")
        return token, key

    def test_jwks_fetch_failure_returns_503(self, monkeypatch):
        from zdrovena.api.auth import _validate_token

        monkeypatch.setenv("AZURE_AUTH_DISABLED", "false")
        monkeypatch.setenv("AZURE_TENANT_ID", "test-tenant")

        with patch(
            "jwt.PyJWKClient.get_signing_key_from_jwt", side_effect=Exception("network error")
        ):
            from fastapi import HTTPException

            with pytest.raises(HTTPException) as exc:
                _validate_token("dummy.token.here")
            assert exc.value.status_code == 503

    def test_invalid_token_returns_401(self, monkeypatch):
        from zdrovena.api.auth import _validate_token

        monkeypatch.setenv("AZURE_AUTH_DISABLED", "false")
        monkeypatch.setenv("AZURE_TENANT_ID", "test-tenant")
        monkeypatch.setenv("AZURE_API_AUDIENCE", "my-api")

        import jwt
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives.asymmetric import rsa

        key = rsa.generate_private_key(65537, 2048, default_backend())
        token = jwt.encode({"sub": "user"}, key, algorithm="RS256")
        signing_key_mock = MagicMock()
        signing_key_mock.key = key.public_key()

        with patch("jwt.PyJWKClient.get_signing_key_from_jwt", return_value=signing_key_mock):
            from fastapi import HTTPException

            # Token has no iss — should fail issuer check
            with pytest.raises(HTTPException) as exc:
                _validate_token(token)
            assert exc.value.status_code == 401

    def test_wrong_issuer_returns_401(self, monkeypatch):
        from zdrovena.api.auth import _validate_token

        monkeypatch.setenv("AZURE_AUTH_DISABLED", "false")
        monkeypatch.setenv("AZURE_TENANT_ID", "correct-tenant")
        monkeypatch.setenv("AZURE_API_AUDIENCE", "my-api")

        import time

        import jwt
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives.asymmetric import rsa

        key = rsa.generate_private_key(65537, 2048, default_backend())
        token = jwt.encode(
            {
                "sub": "user",
                "iss": "https://evil.com/wrong-tenant",
                "aud": "my-api",
                "exp": int(time.time()) + 3600,
            },
            key,
            algorithm="RS256",
        )
        signing_key_mock = MagicMock()
        signing_key_mock.key = key.public_key()

        with patch("jwt.PyJWKClient.get_signing_key_from_jwt", return_value=signing_key_mock):
            from fastapi import HTTPException

            with pytest.raises(HTTPException) as exc:
                _validate_token(token)
            assert exc.value.status_code == 401

    def test_wrong_audience_returns_401(self, monkeypatch):
        from zdrovena.api.auth import _validate_token

        monkeypatch.setenv("AZURE_AUTH_DISABLED", "false")
        monkeypatch.setenv("AZURE_TENANT_ID", "my-tenant")
        monkeypatch.setenv("AZURE_API_AUDIENCE", "my-api")

        import time

        import jwt
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives.asymmetric import rsa

        key = rsa.generate_private_key(65537, 2048, default_backend())
        token = jwt.encode(
            {
                "sub": "user",
                "iss": "https://login.microsoftonline.com/my-tenant/v2.0",
                "aud": "wrong-api",
                "exp": int(time.time()) + 3600,
            },
            key,
            algorithm="RS256",
        )
        signing_key_mock = MagicMock()
        signing_key_mock.key = key.public_key()

        with patch("jwt.PyJWKClient.get_signing_key_from_jwt", return_value=signing_key_mock):
            from fastapi import HTTPException

            with pytest.raises(HTTPException) as exc:
                _validate_token(token)
            assert exc.value.status_code == 401

    def test_valid_token_returns_principal(self, monkeypatch):
        from zdrovena.api.auth import _validate_token

        monkeypatch.setenv("AZURE_AUTH_DISABLED", "false")
        monkeypatch.setenv("AZURE_TENANT_ID", "my-tenant")
        monkeypatch.setenv("AZURE_API_AUDIENCE", "my-api")

        import time

        import jwt
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives.asymmetric import rsa

        key = rsa.generate_private_key(65537, 2048, default_backend())
        token = jwt.encode(
            {
                "sub": "abc123",
                "preferred_username": "user@example.com",
                "iss": "https://login.microsoftonline.com/my-tenant/v2.0",
                "aud": "my-api",
                "exp": int(time.time()) + 3600,
                "roles": ["zdrovena-admin"],
            },
            key,
            algorithm="RS256",
        )
        signing_key_mock = MagicMock()
        signing_key_mock.key = key.public_key()

        with patch("jwt.PyJWKClient.get_signing_key_from_jwt", return_value=signing_key_mock):
            principal = _validate_token(token)
            assert principal.email == "user@example.com"
            assert "zdrovena-admin" in principal.roles
