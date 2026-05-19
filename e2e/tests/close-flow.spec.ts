/**
 * Month-close user journey E2E tests.
 *
 * Requires:
 *   API_URL              — base URL of the backend (e.g. https://zdrovena-api-staging.xxx.azurecontainerapps.io)
 *   AZURE_TEST_BEARER_TOKEN — valid Azure AD access token with the `accountant` app role
 *
 * In CI these are injected by the full-test-suite workflow (staging deploy step).
 * Locally, obtain a token with:
 *   TOKEN=$(az account get-access-token --resource $AZURE_API_AUDIENCE --query accessToken -o tsv)
 *   export API_URL=https://... AZURE_TEST_BEARER_TOKEN=$TOKEN
 */

import { test, expect } from "@playwright/test";

const API_URL = process.env.API_URL ?? "";
const BEARER_TOKEN = process.env.AZURE_TEST_BEARER_TOKEN ?? "";
const HAVE_AUTH = !!API_URL && !!BEARER_TOKEN;

// Month with known staging data (seeded by seed-staging.sh in CI)
const YEAR = 2026;
const MONTH = 4;

test.describe("Month-close user journey (authenticated)", () => {
  test("sales invoice list → dry-run close → verify CloseResponse schema", async ({ request }) => {
    test.skip(!HAVE_AUTH, "API_URL and AZURE_TEST_BEARER_TOKEN required for this test");

    // Step 1: List sales invoices (simulates the UI invoice list view)
    const listRes = await request.get(
      `${API_URL}/api/invoices/sales?year=${YEAR}&month=${MONTH}`,
      { headers: { Authorization: `Bearer ${BEARER_TOKEN}` } },
    );
    expect(listRes.status()).toBe(200);
    const invoices = await listRes.json();
    expect(Array.isArray(invoices)).toBe(true);

    // Step 2: Dry-run close (simulates the user pressing "Run" with dry_run=true)
    const closeRes = await request.post(`${API_URL}/api/close`, {
      headers: { Authorization: `Bearer ${BEARER_TOKEN}` },
      data: { year: YEAR, month: MONTH, dry_run: true, ignore_warnings: true },
    });
    expect(closeRes.status()).toBe(200);

    // Step 3: Verify CloseResponse schema fields are all present
    const report = await closeRes.json();
    const requiredFields = [
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
    ];
    for (const field of requiredFields) {
      expect(report, `Missing field: ${field}`).toHaveProperty(field);
    }

    // Dry run must never send email
    expect(report.email_sent).toBe(false);

    // Type checks for key fields
    expect(typeof report.sales_invoice_count).toBe("number");
    expect(typeof report.has_critical_errors).toBe("boolean");
    expect(Array.isArray(report.warnings)).toBe(true);
    expect(Array.isArray(report.errors)).toBe(true);
    expect(Array.isArray(report.steps_completed)).toBe(true);
  });

  test("close endpoint rejects month=13 with 422", async ({ request }) => {
    test.skip(!HAVE_AUTH, "API_URL and AZURE_TEST_BEARER_TOKEN required for this test");

    const res = await request.post(`${API_URL}/api/close`, {
      headers: { Authorization: `Bearer ${BEARER_TOKEN}` },
      data: { year: 2026, month: 13, dry_run: true },
    });
    expect(res.status()).toBe(422);
  });

  test("close endpoint returns 401 without token", async ({ request }) => {
    test.skip(!API_URL, "API_URL not set");

    const res = await request.post(`${API_URL}/api/close`, {
      data: { year: 2026, month: 4, dry_run: true },
    });
    expect([401, 403]).toContain(res.status());
  });
});
