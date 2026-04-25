"""API ↔ CLI parity tests — placeholder for Faza D.

Once the FastAPI layer is implemented (Faza C) and the CLI becomes
an API client (Faza D), these tests will verify that:

  - POST /close (dry_run=True) returns the same fields as CloseReport
  - GET /preflight returns the same structure as PreflightChecker.run()

For now this file documents the intended contract so the shape is
clear before implementation begins.
"""

from __future__ import annotations

import pytest


@pytest.mark.skip(reason="Placeholder — implement in Faza D when FastAPI layer exists")
class TestApiCliParity:
    """Local mode vs FastAPI TestClient must return identical field sets."""

    def test_close_dry_run_fields_match(self):
        """
        Expected flow (Faza D):

          # Local mode
          from zdrovena.month_closing.orchestrator import MonthCloseOrchestrator
          local_result = MonthCloseOrchestrator(..., dry_run=True).execute()

          # API mode
          from fastapi.testclient import TestClient
          from zdrovena.api.main import app
          client = TestClient(app)
          api_result = client.post("/api/close", json={"year": 2025, "month": 6, "dry_run": True}).json()

          assert set(asdict(local_result).keys()) == set(api_result.keys())
        """
        raise NotImplementedError

    def test_preflight_fields_match(self):
        """
        Expected flow (Faza D):

          # Local mode
          from zdrovena.month_closing.preflight import PreflightChecker
          local = PreflightChecker(...).run()

          # API mode
          api = client.get("/preflight/2025/6").json()

          assert set(local.keys()) == set(api.keys())
        """
        raise NotImplementedError
