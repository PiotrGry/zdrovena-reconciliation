#!/usr/bin/env tsx
/**
 * Smoke test runner — orchestrates all tests, outputs structured JSON report.
 * Add new tests by dropping a file in tests/ that exports a SmokeTest[].
 */

import { writeFileSync } from "fs";
import { join } from "path";
import type { SmokeReport, SmokeTest, TestContext, TestResult } from "./types.js";

// ── Load all test modules ──────────────────────────────────────────────────
import { tests as apiTests } from "./tests/api.js";
import { tests as authTests } from "./tests/auth.js";
import { tests as authRealTests } from "./tests/auth-real.js";
import { tests as frontendTests } from "./tests/frontend.js";
import { tests as businessTests } from "./tests/business.js";

const ALL_TESTS: SmokeTest[] = [
  ...apiTests,
  ...authTests,
  ...authRealTests,
  ...frontendTests,
  ...businessTests,
];

// ── CLI args ───────────────────────────────────────────────────────────────
const args = process.argv.slice(2);
const verbose = args.includes("--verbose");
const strict =
  args.includes("--strict") ||
  (process.env.SMOKE_STRICT ?? "").trim().toLowerCase() === "true";
const outputArg = args.find((a) => a.startsWith("--output"));
const outputFile = outputArg ? outputArg.split("=")[1] ?? args[args.indexOf(outputArg) + 1] : null;
const excludeArg = args.find((a) => a.startsWith("--exclude-test="));
const excludedTests = new Set(excludeArg ? excludeArg.slice("--exclude-test=".length).split(",").map((s) => s.trim()) : []);
const TESTS = excludedTests.size > 0 ? ALL_TESTS.filter((t) => !excludedTests.has(t.name)) : ALL_TESTS;

// ── Build context ──────────────────────────────────────────────────────────
function fetchWithTimeout(url: string, opts: RequestInit & { timeoutMs?: number } = {}): Promise<Response> {
  const { timeoutMs = 10_000, ...fetchOpts } = opts;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  return fetch(url, { ...fetchOpts, signal: controller.signal }).finally(() =>
    clearTimeout(timer)
  );
}

// Lazy-cached viewer access token — fetched once on first call, reused for the run.
let cachedViewerToken: string | null | undefined;
let cachedAccountantToken: string | null | undefined;
async function getViewerToken(): Promise<string | null> {
  if (cachedViewerToken !== undefined) return cachedViewerToken;
  // Pre-fetched token (e.g. from `az account get-access-token`) takes priority
  if (process.env.SMOKE_VIEWER_TOKEN) {
    cachedViewerToken = process.env.SMOKE_VIEWER_TOKEN;
    return cachedViewerToken;
  }
  const tenant = process.env.AZURE_TENANT_ID?.trim();
  const clientId = process.env.SMOKE_SP_CLIENT_ID?.trim();
  const clientSecret = process.env.SMOKE_SP_CLIENT_SECRET;
  const apiClientId = process.env.AZURE_API_CLIENT_ID?.trim();
  if (!tenant || !clientId || !clientSecret || !apiClientId) {
    cachedViewerToken = null;
    return null;
  }
  const body = new URLSearchParams({
    grant_type: "client_credentials",
    client_id: clientId,
    client_secret: clientSecret,
    scope: `api://${apiClientId}/.default`,
  });
  const res = await fetchWithTimeout(
    `https://login.microsoftonline.com/${tenant}/oauth2/v2.0/token`,
    { method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" }, body, timeoutMs: 10_000 },
  );
  if (!res.ok) {
    cachedViewerToken = null;
    return null;
  }
  const json = await res.json() as { access_token?: string };
  cachedViewerToken = json.access_token ?? null;
  return cachedViewerToken;
}

async function getAccountantToken(): Promise<string | null> {
  if (cachedAccountantToken !== undefined) return cachedAccountantToken;
  // Pre-fetched token (e.g. from `az account get-access-token`) takes priority
  if (process.env.SMOKE_ACCOUNTANT_TOKEN) {
    cachedAccountantToken = process.env.SMOKE_ACCOUNTANT_TOKEN;
    return cachedAccountantToken;
  }
  const tenant = process.env.AZURE_TENANT_ID?.trim();
  const clientId = process.env.SMOKE_ACCOUNTANT_SP_CLIENT_ID?.trim();
  const clientSecret = process.env.SMOKE_ACCOUNTANT_SP_CLIENT_SECRET;
  const apiClientId = process.env.AZURE_API_CLIENT_ID?.trim();
  if (!tenant || !clientId || !clientSecret || !apiClientId) {
    cachedAccountantToken = null;
    return null;
  }
  const body = new URLSearchParams({
    grant_type: "client_credentials",
    client_id: clientId,
    client_secret: clientSecret,
    scope: `api://${apiClientId}/.default`,
  });
  const res = await fetchWithTimeout(
    `https://login.microsoftonline.com/${tenant}/oauth2/v2.0/token`,
    { method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" }, body, timeoutMs: 10_000 },
  );
  if (!res.ok) { cachedAccountantToken = null; return null; }
  const json = await res.json() as { access_token?: string };
  cachedAccountantToken = json.access_token ?? null;
  return cachedAccountantToken;
}

const ctx: TestContext = {
  apiUrl: process.env.API_URL ?? "http://localhost:8000",
  swaUrl: process.env.SWA_URL ?? "http://localhost:5173",
  azureTenantId: process.env.AZURE_TENANT_ID ?? "",
  azureClientId: process.env.AZURE_CLIENT_ID ?? "",
  azureApiClientId: process.env.AZURE_API_CLIENT_ID ?? "",
  azureSubscriptionId: process.env.AZURE_SUBSCRIPTION_ID ?? "",
  smokeSpClientId: process.env.SMOKE_SP_CLIENT_ID ?? "",
  smokeSpClientSecret: process.env.SMOKE_SP_CLIENT_SECRET ?? "",
  smokeAccountantSpClientId: process.env.SMOKE_ACCOUNTANT_SP_CLIENT_ID ?? "",
  smokeAccountantSpClientSecret: process.env.SMOKE_ACCOUNTANT_SP_CLIENT_SECRET ?? "",
  verbose,
  strict,
  fetch: fetchWithTimeout,
  getViewerToken,
  getAccountantToken,
};

// ── Run tests ──────────────────────────────────────────────────────────────
async function run(): Promise<void> {
  const startMs = Date.now();
  const results: TestResult[] = [];

  console.log(`\nSmoke test suite — ${TESTS.length} tests${excludedTests.size > 0 ? ` (${excludedTests.size} excluded)` : ""}${strict ? " [STRICT]" : ""}`);
  console.log(`API:     ${ctx.apiUrl}`);
  console.log(`SWA:     ${ctx.swaUrl}`);
  console.log("─".repeat(60));

  for (const test of TESTS) {
    const t0 = Date.now();
    let result: TestResult;
    try {
      result = await test.run(ctx);
    } catch (err: unknown) {
      result = {
        name: test.name,
        category: test.category,
        status: "FAIL",
        duration_ms: Date.now() - t0,
        evidence: "",
        error: err instanceof Error ? err.message : String(err),
      };
    }

    results.push(result);
    const icon = result.status === "PASS" ? "✅" : result.status === "SKIP" ? "⏭️ " : "❌";
    console.log(`${icon} [${result.category}] ${result.name} (${result.duration_ms}ms)`);
    if (ctx.verbose || result.status === "FAIL") {
      if (result.evidence) console.log(`   evidence: ${result.evidence}`);
      if (result.error) console.log(`   error: ${result.error}`);
    }
  }

  const passed = results.filter((r) => r.status === "PASS").length;
  const failed = results.filter((r) => r.status === "FAIL").length;
  const skipped = results.filter((r) => r.status === "SKIP").length;

  console.log("─".repeat(60));
  console.log(`Results: ${passed} passed, ${failed} failed, ${skipped} skipped`);

  const report: SmokeReport = {
    timestamp: new Date().toISOString(),
    api_url: ctx.apiUrl,
    swa_url: ctx.swaUrl,
    strict,
    total: results.length,
    passed,
    failed,
    skipped,
    duration_ms: Date.now() - startMs,
    tests: results,
  };

  if (outputFile) {
    writeFileSync(outputFile, JSON.stringify(report, null, 2));
    console.log(`\nReport written to ${outputFile}`);
  } else {
    process.stdout.write("\n" + JSON.stringify(report, null, 2) + "\n");
  }

  process.exit(failed > 0 ? 1 : 0);
}

run().catch((err) => {
  console.error("Runner crashed:", err);
  process.exit(1);
});
