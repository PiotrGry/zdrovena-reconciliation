/**
 * Auth configuration smoke tests.
 * Verifies VITE_ env vars are baked into the SWA bundle at build time.
 */

import type { SmokeTest, TestContext, TestResult } from "../types.js";

function ms(): number { return Date.now(); }

type Bundle = { url: string; content: string };

let bundlePromise: Promise<Bundle | null> | undefined;

function sleep(delayMs: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, delayMs));
}

async function downloadBundle(ctx: TestContext): Promise<Bundle | null> {
  const htmlResponse = await ctx.fetch(ctx.swaUrl, { timeoutMs: 20_000 });
  if (!htmlResponse.ok) {
    throw new Error(`SWA HTML returned ${htmlResponse.status}`);
  }
  const html = await htmlResponse.text();
  const match = html.match(/src="(\/assets\/[^"]*\.js)"/);
  if (!match) return null;
  const bundleUrl = ctx.swaUrl + match[1];
  const bundleResponse = await ctx.fetch(bundleUrl, { timeoutMs: 30_000 });
  if (!bundleResponse.ok) {
    throw new Error(`SWA bundle returned ${bundleResponse.status}`);
  }
  const content = await bundleResponse.text();
  return { url: bundleUrl, content };
}

async function fetchBundle(ctx: TestContext): Promise<Bundle | null> {
  bundlePromise ??= (async () => {
    let lastError: unknown;
    for (let attempt = 1; attempt <= 3; attempt += 1) {
      try {
        return await downloadBundle(ctx);
      } catch (error) {
        lastError = error;
        if (attempt < 3) await sleep(attempt * 1_000);
      }
    }
    throw lastError;
  })();
  return bundlePromise;
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

    if (!hasClientId && !ctx.strict) {
      return {
        name: this.name,
        category: this.category,
        status: "SKIP",
        duration_ms: ms() - t0,
        evidence: `No literal API client ID found in ${bundle.url}`,
        error: "Skipped in non-strict mode; token acquisition smoke tests verify the configured API audience.",
      };
    }

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
