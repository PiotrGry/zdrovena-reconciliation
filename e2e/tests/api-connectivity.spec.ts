/**
 * API connectivity E2E tests.
 * Verifies the SWA frontend can reach the backend Container App via the /api proxy.
 */

import { test, expect } from "@playwright/test";

const API_URL = process.env.API_URL ?? "";

test.describe("API connectivity", () => {
  test("backend /health is reachable", async ({ request }) => {
    test.skip(!API_URL, "API_URL not set — skipping direct API tests");
    const res = await request.get(`${API_URL}/health`);
    expect(res.status()).toBe(200);
  });

  test("unauthenticated /files returns 401 not 500", async ({ request }) => {
    test.skip(!API_URL, "API_URL not set — skipping direct API tests");
    const res = await request.get(`${API_URL}/api/files`);
    expect([401, 403]).toContain(res.status());
  });

  test("unauthenticated /invoices/sales returns 401 not 500", async ({ request }) => {
    test.skip(!API_URL, "API_URL not set — skipping direct API tests");
    const res = await request.get(`${API_URL}/api/invoices/sales?year=2026&month=4`);
    expect([401, 403]).toContain(res.status());
  });
});
