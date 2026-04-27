/**
 * Real auth tests — exercise the full token-acquisition + role-based access
 * path. Without these, the rest of smoke only proves "endpoints reject anon",
 * never that they actually work for an authenticated user.
 *
 * Uses a dedicated Service Principal (zdrovena-smoke-tester) with the
 * zdrovena-viewer app role assigned. Credentials passed via SMOKE_SP_CLIENT_ID
 * and SMOKE_SP_CLIENT_SECRET env vars. If unset → all tests SKIP (still safe
 * for local dev runs).
 */

import type { SmokeTest, TestContext, TestResult } from "../types.js";

function ms(): number { return Date.now(); }

function decodeJwt(token: string): Record<string, unknown> {
  const [, payload] = token.split(".");
  const padded = payload + "=".repeat((4 - payload.length % 4) % 4);
  return JSON.parse(Buffer.from(padded, "base64url").toString("utf8")) as Record<string, unknown>;
}

const tokenAcquirable: SmokeTest = {
  name: "auth.viewer_token_acquirable",
  category: "auth",
  async run(ctx: TestContext): Promise<TestResult> {
    const t0 = ms();
    if (!ctx.smokeSpClientId || !ctx.smokeSpClientSecret) {
      return { name: this.name, category: this.category, status: "SKIP", duration_ms: ms() - t0, evidence: "SMOKE_SP_* env vars not set" };
    }
    const token = await ctx.getViewerToken();
    return {
      name: this.name,
      category: this.category,
      status: token ? "PASS" : "FAIL",
      duration_ms: ms() - t0,
      evidence: token ? `token length: ${token.length}` : "no token returned",
      error: !token ? "Failed to acquire token via client_credentials — check SP role assignment + secret" : undefined,
    };
  },
};

const tokenHasViewerRole: SmokeTest = {
  name: "auth.token_carries_viewer_role",
  category: "auth",
  async run(ctx: TestContext): Promise<TestResult> {
    const t0 = ms();
    const token = await ctx.getViewerToken();
    if (!token) {
      return { name: this.name, category: this.category, status: "SKIP", duration_ms: ms() - t0, evidence: "no token (smoke SP not configured)" };
    }
    const claims = decodeJwt(token);
    const roles = (claims.roles as string[] | undefined) ?? [];
    const ok = roles.includes("zdrovena-viewer");
    return {
      name: this.name,
      category: this.category,
      status: ok ? "PASS" : "FAIL",
      duration_ms: ms() - t0,
      evidence: `roles=${JSON.stringify(roles)}, aud=${claims.aud}, ver=${claims.ver}`,
      error: !ok ? `Expected zdrovena-viewer in roles, got ${JSON.stringify(roles)}` : undefined,
    };
  },
};

const filesAuthenticatedReturns200: SmokeTest = {
  name: "api.files_authenticated_returns_200",
  category: "api",
  async run(ctx: TestContext): Promise<TestResult> {
    const t0 = ms();
    const token = await ctx.getViewerToken();
    if (!token) {
      return { name: this.name, category: this.category, status: "SKIP", duration_ms: ms() - t0, evidence: "no token" };
    }
    const res = await ctx.fetch(`${ctx.apiUrl}/api/files`, {
      headers: { Authorization: `Bearer ${token}` },
      timeoutMs: 8_000,
    });
    return {
      name: this.name,
      category: this.category,
      status: res.status === 200 ? "PASS" : "FAIL",
      duration_ms: ms() - t0,
      evidence: `HTTP ${res.status}`,
      error: res.status !== 200 ? `Expected 200, got ${res.status} — viewer should read /api/files` : undefined,
    };
  },
};

const closeStateAuthenticatedReturns200: SmokeTest = {
  name: "api.close_state_authenticated_returns_200",
  category: "api",
  async run(ctx: TestContext): Promise<TestResult> {
    const t0 = ms();
    const token = await ctx.getViewerToken();
    if (!token) {
      return { name: this.name, category: this.category, status: "SKIP", duration_ms: ms() - t0, evidence: "no token" };
    }
    const res = await ctx.fetch(`${ctx.apiUrl}/api/close/state?year=2026&month=4`, {
      headers: { Authorization: `Bearer ${token}` },
      timeoutMs: 8_000,
    });
    return {
      name: this.name,
      category: this.category,
      status: res.status === 200 ? "PASS" : "FAIL",
      duration_ms: ms() - t0,
      evidence: `HTTP ${res.status}`,
      error: res.status !== 200 ? `Expected 200, got ${res.status} — viewer should read /api/close/state` : undefined,
    };
  },
};

const closePostForbiddenForViewer: SmokeTest = {
  name: "api.close_post_forbidden_for_viewer",
  category: "api",
  async run(ctx: TestContext): Promise<TestResult> {
    const t0 = ms();
    const token = await ctx.getViewerToken();
    if (!token) {
      return { name: this.name, category: this.category, status: "SKIP", duration_ms: ms() - t0, evidence: "no token" };
    }
    // Viewer must NOT be able to trigger close — only accountant/admin.
    const res = await ctx.fetch(`${ctx.apiUrl}/api/close`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ year: 2026, month: 4, dry_run: true }),
      timeoutMs: 8_000,
    });
    return {
      name: this.name,
      category: this.category,
      status: res.status === 403 ? "PASS" : "FAIL",
      duration_ms: ms() - t0,
      evidence: `HTTP ${res.status}`,
      error: res.status !== 403 ? `Expected 403 for viewer POST /api/close, got ${res.status} — RBAC may be broken` : undefined,
    };
  },
};

export const tests: SmokeTest[] = [
  tokenAcquirable,
  tokenHasViewerRole,
  filesAuthenticatedReturns200,
  closeStateAuthenticatedReturns200,
  closePostForbiddenForViewer,
];
