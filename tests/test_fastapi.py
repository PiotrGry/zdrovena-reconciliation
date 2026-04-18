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
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

# Ensure auth is disabled globally for the test module
os.environ.setdefault("AZURE_AUTH_DISABLED", "true")

from zdrovena.api.main import app
from zdrovena.api.auth import Principal, get_current_principal
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

        resp = c.get("/files/invoices/sales/faktura.pdf")

        assert resp.status_code == 200
        assert resp.content == b"PDF data"
        assert "attachment" in resp.headers["content-disposition"]
        assert "faktura.pdf" in resp.headers["content-disposition"]

    def test_download_missing_file_returns_404(self, api):
        c, _ = api
        resp = c.get("/files/missing/file.pdf")
        assert resp.status_code == 404

    def test_download_path_traversal_rejected(self, api):
        c, _ = api
        resp = c.get("/files/../etc/passwd")
        assert resp.status_code in (400, 404)

    def test_content_type_pdf(self, api):
        c, storage = api
        storage.root.mkdir(parents=True, exist_ok=True)
        (storage.root / "doc.pdf").write_bytes(b"%PDF")

        resp = c.get("/files/doc.pdf")

        assert "pdf" in resp.headers.get("content-type", "").lower()


# ── /files — list ─────────────────────────────────────────────────────────────

class TestFilesList:
    def test_list_empty(self, api):
        c, _ = api
        resp = c.get("/files/")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_returns_entries(self, api):
        c, storage = api
        (storage.root / "invoices").mkdir(parents=True)
        (storage.root / "invoices/a.pdf").write_bytes(b"a")
        (storage.root / "invoices/b.pdf").write_bytes(b"b")

        resp = c.get("/files/", params={"prefix": "invoices"})

        assert resp.status_code == 200
        keys = {f["key"] for f in resp.json()}
        assert "invoices/a.pdf" in keys
        assert "invoices/b.pdf" in keys

    def test_list_entry_shape(self, api):
        c, storage = api
        storage.root.mkdir(parents=True, exist_ok=True)
        (storage.root / "x.pdf").write_bytes(b"x")

        entry = c.get("/files/").json()[0]

        assert {"key", "size", "last_modified"} <= entry.keys()


# ── /files — auth enforcement ─────────────────────────────────────────────────

class TestFilesAuth:
    def test_no_token_returns_401_when_auth_enabled(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AZURE_AUTH_DISABLED", "false")
        storage = LocalStorageService(root=tmp_path / "storage")
        with patch("zdrovena.api.deps._storage_singleton", return_value=storage):
            with TestClient(app, raise_server_exceptions=True) as c:
                resp = c.get("/files/any/file.pdf")
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
                    resp = c.get("/files/x.pdf")
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
            resp = c.post("/close", json={"year": 2026, "month": 3, "dry_run": True})

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
            data = c.post("/close", json={"year": 2026, "month": 3}).json()

        assert data["sales_invoice_count"] == 3
        assert data["sales_gross_total"] == "1500.00"
        assert data["cost_invoice_count"] == 7
        assert data["warnings"] == ["w1"]
        assert data["steps_completed"] == ["step_1"]

    def test_invalid_month_returns_422(self, api):
        c, _ = api
        resp = c.post("/close", json={"year": 2026, "month": 13})
        assert resp.status_code == 422

    def test_invalid_year_returns_422(self, api):
        c, _ = api
        resp = c.post("/close", json={"year": 1999, "month": 3})
        assert resp.status_code == 422

    def test_orchestrator_value_error_returns_400(self, api):
        c, _ = api
        with patch(
            "zdrovena.api.routers.close.MonthCloseOrchestrator",
            side_effect=ValueError("bad"),
        ):
            resp = c.post("/close", json={"year": 2026, "month": 3})
        assert resp.status_code == 400

    def test_close_requires_auth_when_enabled(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AZURE_AUTH_DISABLED", "false")
        storage = LocalStorageService(root=tmp_path / "storage")
        with patch("zdrovena.api.deps._storage_singleton", return_value=storage):
            with TestClient(app, raise_server_exceptions=True) as c:
                resp = c.post("/close", json={"year": 2026, "month": 3})
        assert resp.status_code == 401

    def test_close_viewer_role_returns_403(self, tmp_path):
        viewer = Principal(sub="u", email="u@x.com", roles=["zdrovena-viewer"])
        storage = LocalStorageService(root=tmp_path / "storage")
        app.dependency_overrides[get_current_principal] = lambda: viewer
        try:
            with patch("zdrovena.api.deps._storage_singleton", return_value=storage):
                with TestClient(app, raise_server_exceptions=False) as c:
                    resp = c.post("/close", json={"year": 2026, "month": 3})
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
