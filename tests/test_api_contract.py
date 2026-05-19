"""API contract test — pins the CloseResponse schema.

Any field addition, removal, or rename will fail this test.
Update EXPECTED_FIELDS intentionally when the schema changes.
"""

from __future__ import annotations


from zdrovena.api.models import CloseResponse

EXPECTED_FIELDS: frozenset[str] = frozenset(
    {
        "sales_invoice_count",
        "sales_gross_total",
        "sales_pdfs_downloaded",
        "cost_invoice_count",
        "cost_found_vendors",
        "cost_missing_vendors",
        "ksef_count",
        "bank_statement_found",
        "zip_path",
        "zip_files",
        "email_sent",
        "warnings",
        "errors",
        "steps_completed",
        "has_critical_errors",
        "log_lines",
    }
)


class TestCloseResponseContract:
    def test_no_fields_removed(self):
        actual = set(CloseResponse.model_fields)
        missing = EXPECTED_FIELDS - actual
        assert not missing, f"CloseResponse fields removed (update contract): {missing}"

    def test_no_fields_added(self):
        actual = set(CloseResponse.model_fields)
        added = actual - EXPECTED_FIELDS
        assert not added, f"New CloseResponse fields detected (update contract): {added}"
