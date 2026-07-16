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

// seed-staging.sh always prepares the previous calendar month.
const now = new Date();
const YEAR = now.getUTCMonth() === 0 ? now.getUTCFullYear() - 1 : now.getUTCFullYear();
const MONTH = now.getUTCMonth() === 0 ? 12 : now.getUTCMonth();

test.describe("Month-close user journey (authenticated)", () => {
  test("check → collect stages → reviewable package", async ({ request }) => {
    test.skip(!HAVE_AUTH, "API_URL and AZURE_TEST_BEARER_TOKEN required for this test");

    const headers = {
      Authorization: `Bearer ${BEARER_TOKEN}`,
      "Content-Type": "application/json",
    };
    const reset = await request.post(`${API_URL}/api/close/workflow/reset`, {
      headers,
      data: { year: YEAR, month: MONTH },
    });
    expect(reset.status()).toBe(200);

    for (const action of ["check", "sales", "costs", "reports", "bank", "package"]) {
      const response = await request.post(
        `${API_URL}/api/close/workflow/actions/${action}`,
        {
          headers,
          data: { year: YEAR, month: MONTH },
        },
      );
      expect(response.status(), `${action} request failed`).toBe(200);
      const run = await response.json();
      expect(run.steps[action].status, `${action} did not finish`).toBe("done");
      expect(run.active_action).toBeNull();
      if (action === "check") {
        expect(run.documents.length).toBeGreaterThan(0);
      }
      if (action === "package") {
        expect(run.status).toBe("package_ready");
        expect(run.artifacts.some((artifact: { kind: string }) => artifact.kind === "package")).toBe(true);
      }
    }
  });

  test("legacy dry-run remains read-only and schema-compatible", async ({ request }) => {
    test.skip(!HAVE_AUTH, "API_URL and AZURE_TEST_BEARER_TOKEN required for this test");

    const closeRes = await request.post(`${API_URL}/api/close`, {
      headers: { Authorization: `Bearer ${BEARER_TOKEN}` },
      data: { year: YEAR, month: MONTH, dry_run: true, ignore_warnings: true },
    });
    expect(closeRes.status()).toBe(200);

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

    expect(report.email_sent).toBe(false);

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
