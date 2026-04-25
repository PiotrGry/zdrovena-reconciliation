/**
 * Auth configuration smoke tests.
 * Verifies VITE_ env vars are baked into the SWA bundle at build time.
 */

import type { SmokeTest, TestContext, TestResult } from "../types.js";

function ms(): number { return Date.now(); }

async function fetchBundle(ctx: TestContext): Promise<{ url: string; content: string } | null> {
  const html = await ctx.fetch(ctx.swaUrl, { timeoutMs: 15_000 }).then((r) => r.text());
  const match = html.match(/src="(\/assets\/[^"]*\.js)"/);
  if (!match) return null;
  const bundleUrl = ctx.swaUrl + match[1];
  const content = await ctx.fetch(bundleUrl, { timeoutMs: 20_000 }).then((r) => r.text());
  return { url: bundleUrl, content };
}

const swaLoads: SmokeTest = {
  name: "auth.swa_returns_200",
  category: "auth",
  async run(ctx: TestContext): Promise<TestResult> {
    const t0 = ms();
    const res = await ctx.fetch(ctx.swaUrl, { timeoutMs: 15_000 });
    return {
      name: this.name,
      category: this.category,
      status: res.status === 200 ? "PASS" : "FAIL",
      duration_ms: ms() - t0,
      evidence: `HTTP ${res.status}`,
      error: res.status !== 200 ? `SWA returned ${res.status}` : undefined,
    };
  },
};

const tenantGuidInBundle: SmokeTest = {
  name: "auth.tenant_guid_baked_into_bundle",
  category: "auth",
  async run(ctx: TestContext): Promise<TestResult> {
    const t0 = ms();
    const bundle = await fetchBundle(ctx);
    if (!bundle) {
      return { name: this.name, category: this.category, status: "FAIL", duration_ms: ms() - t0, evidence: "", error: "No JS bundle found in SWA HTML" };
    }

    // Must NOT be undefined literal
    if (bundle.content.includes("microsoftonline.com/undefined")) {
      return { name: this.name, category: this.category, status: "FAIL", duration_ms: ms() - t0, evidence: bundle.url, error: "VITE_AZURE_TENANT_ID compiled as 'undefined' — env var missing at build time" };
    }

    // Must contain a real GUID pattern
    const hasGuid = /microsoftonline\.com\/[0-9a-f]{8}-[0-9a-f]{4}/.test(bundle.content) ||
      /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/.test(bundle.content);

    return {
      name: this.name,
      category: this.category,
      status: hasGuid ? "PASS" : "FAIL",
      duration_ms: ms() - t0,
      evidence: hasGuid ? `GUID found in ${bundle.url}` : `No GUID in ${bundle.url}`,
      error: !hasGuid ? "No tenant GUID found in bundle — auth will fail" : undefined,
    };
  },
};

const clientIdInBundle: SmokeTest = {
  name: "auth.api_client_id_baked_into_bundle",
  category: "auth",
  async run(ctx: TestContext): Promise<TestResult> {
    const t0 = ms();
    const bundle = await fetchBundle(ctx);
    if (!bundle) {
      return { name: this.name, category: this.category, status: "FAIL", duration_ms: ms() - t0, evidence: "", error: "No JS bundle found" };
    }

    if (bundle.content.includes("api://undefined")) {
      return { name: this.name, category: this.category, status: "FAIL", duration_ms: ms() - t0, evidence: bundle.url, error: "VITE_AZURE_API_CLIENT_ID compiled as 'undefined'" };
    }

    const hasClientId = bundle.content.includes(ctx.azureApiClientId) ||
      /api:\/\/[0-9a-f]{8}-[0-9a-f]{4}/.test(bundle.content);

    return {
      name: this.name,
      category: this.category,
      status: hasClientId ? "PASS" : "FAIL",
      duration_ms: ms() - t0,
      evidence: hasClientId ? "API client ID present in bundle" : "API client ID missing",
      error: !hasClientId ? "API client ID not found — token scope will be wrong" : undefined,
    };
  },
};

export const tests: SmokeTest[] = [
  swaLoads,
  tenantGuidInBundle,
  clientIdInBundle,
];
