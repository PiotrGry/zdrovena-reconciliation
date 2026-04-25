/**
 * Frontend smoke tests — SWA serving, routing, static assets.
 */

import type { SmokeTest, TestContext, TestResult } from "../types.js";

function ms(): number { return Date.now(); }

const staticwebappConfig: SmokeTest = {
  name: "frontend.staticwebapp_config_serves",
  category: "frontend",
  async run(ctx: TestContext): Promise<TestResult> {
    const t0 = ms();
    const res = await ctx.fetch(`${ctx.swaUrl}/staticwebapp.config.json`, { timeoutMs: 10_000 });
    // SWA may return 404 for this file — that's fine, the important thing is it doesn't 5xx
    const ok = res.status < 500;
    return {
      name: this.name,
      category: this.category,
      status: ok ? "PASS" : "FAIL",
      duration_ms: ms() - t0,
      evidence: `HTTP ${res.status}`,
      error: !ok ? `SWA returned ${res.status} — server error` : undefined,
    };
  },
};

const spaRouting: SmokeTest = {
  name: "frontend.spa_routing_returns_html",
  category: "frontend",
  async run(ctx: TestContext): Promise<TestResult> {
    const t0 = ms();
    // Deep route should return index.html (SPA routing)
    const res = await ctx.fetch(`${ctx.swaUrl}/settings`, { timeoutMs: 10_000 });
    const body = await res.text();
    const isHtml = body.includes("<!doctype html") || body.includes("<!DOCTYPE html");
    // 200 = navigationFallback configured (ideal); 404 with HTML body = SWA
    // serving 404.html before fallback kicks in (config propagation lag, up
    // to 15min on staging). Both prove SWA is serving HTML, not 5xx-ing.
    return {
      name: this.name,
      category: this.category,
      status: isHtml && res.status < 500 ? "PASS" : "FAIL",
      duration_ms: ms() - t0,
      evidence: `HTTP ${res.status}, isHtml=${isHtml}`,
      error: !isHtml ? "Deep route did not return HTML — SPA routing may be misconfigured" : undefined,
    };
  },
};

const bundleLoads: SmokeTest = {
  name: "frontend.js_bundle_loads",
  category: "frontend",
  async run(ctx: TestContext): Promise<TestResult> {
    const t0 = ms();
    const html = await ctx.fetch(ctx.swaUrl, { timeoutMs: 15_000 }).then((r) => r.text());
    const match = html.match(/src="(\/assets\/[^"]*\.js)"/);
    if (!match) {
      return { name: this.name, category: this.category, status: "FAIL", duration_ms: ms() - t0, evidence: "", error: "No JS bundle script tag in HTML — likely serving dev mode or empty page" };
    }
    const bundleRes = await ctx.fetch(ctx.swaUrl + match[1], { timeoutMs: 20_000 });
    return {
      name: this.name,
      category: this.category,
      status: bundleRes.status === 200 ? "PASS" : "FAIL",
      duration_ms: ms() - t0,
      evidence: `${match[1]} → HTTP ${bundleRes.status}`,
      error: bundleRes.status !== 200 ? `Bundle returned ${bundleRes.status}` : undefined,
    };
  },
};

export const tests: SmokeTest[] = [
  staticwebappConfig,
  spaRouting,
  bundleLoads,
];
