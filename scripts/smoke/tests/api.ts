/**
 * API health and endpoint smoke tests.
 * Tests what the backend serves — does it respond, correctly, within time budget?
 */

import type { SmokeTest, TestContext, TestResult } from "../types.js";

function ms(): number { return Date.now(); }

const health: SmokeTest = {
  name: "api.health_returns_200",
  category: "api",
  async run(ctx: TestContext): Promise<TestResult> {
    const t0 = ms();
    const res = await ctx.fetch(`${ctx.apiUrl}/health`, { timeoutMs: 8_000 });
    return {
      name: this.name,
      category: this.category,
      status: res.status === 200 ? "PASS" : "FAIL",
      duration_ms: ms() - t0,
      evidence: `HTTP ${res.status}`,
      error: res.status !== 200 ? `Expected 200, got ${res.status}` : undefined,
    };
  },
};

const healthResponseTime: SmokeTest = {
  name: "api.health_under_2s",
  category: "api",
  async run(ctx: TestContext): Promise<TestResult> {
    const t0 = ms();
    await ctx.fetch(`${ctx.apiUrl}/health`, { timeoutMs: 5_000 });
    const duration = ms() - t0;
    return {
      name: this.name,
      category: this.category,
      status: duration < 2_000 ? "PASS" : "FAIL",
      duration_ms: duration,
      evidence: `${duration}ms`,
      error: duration >= 2_000 ? `Response took ${duration}ms, threshold 2000ms` : undefined,
    };
  },
};

const docsAvailable: SmokeTest = {
  name: "api.docs_returns_200",
  category: "api",
  async run(ctx: TestContext): Promise<TestResult> {
    const t0 = ms();
    const res = await ctx.fetch(`${ctx.apiUrl}/docs`, { timeoutMs: 8_000 });
    return {
      name: this.name,
      category: this.category,
      status: res.status === 200 ? "PASS" : "FAIL",
      duration_ms: ms() - t0,
      evidence: `HTTP ${res.status}`,
      error: res.status !== 200 ? `Expected 200, got ${res.status}` : undefined,
    };
  },
};

const unauthenticatedFilesReturns401: SmokeTest = {
  name: "api.files_unauthenticated_returns_401",
  category: "api",
  async run(ctx: TestContext): Promise<TestResult> {
    const t0 = ms();
    const res = await ctx.fetch(`${ctx.apiUrl}/api/files`, { timeoutMs: 8_000 });
    const ok = res.status === 401 || res.status === 403;
    return {
      name: this.name,
      category: this.category,
      status: ok ? "PASS" : "FAIL",
      duration_ms: ms() - t0,
      evidence: `HTTP ${res.status}`,
      error: !ok ? `Expected 401/403, got ${res.status} — unauthenticated request should be rejected` : undefined,
    };
  },
};

const unauthenticatedInvoicesReturns401: SmokeTest = {
  name: "api.invoices_unauthenticated_returns_401",
  category: "api",
  async run(ctx: TestContext): Promise<TestResult> {
    const t0 = ms();
    const res = await ctx.fetch(`${ctx.apiUrl}/api/invoices/sales?year=2026&month=4`, { timeoutMs: 8_000 });
    const ok = res.status === 401 || res.status === 403;
    return {
      name: this.name,
      category: this.category,
      status: ok ? "PASS" : "FAIL",
      duration_ms: ms() - t0,
      evidence: `HTTP ${res.status}`,
      error: !ok ? `Expected 401/403, got ${res.status}` : undefined,
    };
  },
};

export const tests: SmokeTest[] = [
  health,
  healthResponseTime,
  docsAvailable,
  unauthenticatedFilesReturns401,
  unauthenticatedInvoicesReturns401,
];
