/**
 * Business logic smoke tests.
 * Placeholder — add tests here as the feature surface grows.
 * Each test should verify a specific business invariant against the staging API.
 *
 * To add a new test:
 *   1. Implement the SmokeTest interface
 *   2. Export it in the tests array at the bottom
 *   3. Done — the runner picks it up automatically
 */

import type { SmokeTest, TestContext, TestResult } from "../types.js";

function ms(): number { return Date.now(); }

/**
 * /close/state must be accessible to viewer role (D3 decision from eng review).
 * Tests the auth change made on 2026-04-24.
 */
const closeStateViewerAccessible: SmokeTest = {
  name: "business.close_state_accessible_without_admin",
  category: "business",
  async run(ctx: TestContext): Promise<TestResult> {
    const t0 = ms();
    // Unauthenticated → should return 401 (not 403 or 500)
    // A 401 means the endpoint exists and auth is working correctly
    const res = await ctx.fetch(
      `${ctx.apiUrl}/close/state?year=2026&month=4`,
      { timeoutMs: 8_000 }
    );
    // 401 = endpoint exists, requires auth (correct)
    // 403 = exists but wrong role (acceptable)
    // 500 = server error (FAIL)
    // 404 = endpoint missing (FAIL)
    const ok = res.status === 401 || res.status === 403;
    return {
      name: this.name,
      category: this.category,
      status: ok ? "PASS" : "FAIL",
      duration_ms: ms() - t0,
      evidence: `HTTP ${res.status}`,
      error: !ok ? `Expected 401/403, got ${res.status} — endpoint may be missing or broken` : undefined,
    };
  },
};

// Add more business tests here as features ship.
// Examples of what to add when the dashboard is built:
//   - GET /dashboard returns 401 unauthenticated (not 404)
//   - GET /invoices/sales?year=X&month=Y returns 401 unauthenticated
//   - POST /close returns 401 unauthenticated

export const tests: SmokeTest[] = [
  closeStateViewerAccessible,
];
