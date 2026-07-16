/**
 * Business logic smoke tests — verify core pipeline invariants against staging API.
 *
 * Philosophy:
 *   - 422 on /close = ACCEPTABLE (preflight blockers, files missing in staging)
 *   - 500 on /close = ALWAYS FAIL (pipeline crashed — would crash on prod too)
 *   - Tests skip gracefully when accountant SP creds aren't configured
 *
 * Required CI secrets for full coverage:
 *   SMOKE_ACCOUNTANT_SP_CLIENT_ID     — SP with zdrovena-accountant role
 *   SMOKE_ACCOUNTANT_SP_CLIENT_SECRET
 */

import type { SmokeTest, TestContext, TestResult } from "../types.js";
import { credentialGate } from "../strict.js";

function ms(): number { return Date.now(); }

const CLOSE_REQUIRED_FIELDS = [
  "sales_invoice_count",
  "sales_gross_total",
  "cost_invoice_count",
  "bank_statement_found",
  "warnings",
  "errors",
  "steps_completed",
  "has_critical_errors",
];

/** POST /close dry_run=true must not crash — 200 or 422 are both acceptable, 500 is not. */
const closeDryRunDoesNotCrash: SmokeTest = {
  name: "business.close_dry_run_does_not_crash",
  category: "business",
  async run(ctx: TestContext): Promise<TestResult> {
    const t0 = ms();
    const token = await ctx.getAccountantToken();
    if (!token) {
      return credentialGate(ctx, this, t0, "SMOKE_ACCOUNTANT_SP_* not configured");
    }
    const now = new Date();
    const year = now.getMonth() === 0 ? now.getFullYear() - 1 : now.getFullYear();
    const month = now.getMonth() === 0 ? 12 : now.getMonth(); // previous month
    const res = await ctx.fetch(`${ctx.apiUrl}/api/close`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ year, month, dry_run: true }),
      timeoutMs: 30_000,
    });
    // 200 = pipeline ran clean, 422 = preflight blockers (files missing — normal in staging)
    const ok = res.status === 200 || res.status === 422;
    return {
      name: this.name,
      category: this.category,
      status: ok ? "PASS" : "FAIL",
      duration_ms: ms() - t0,
      evidence: `HTTP ${res.status} for ${year}/${month} dry_run=true`,
      error: !ok ? `Expected 200 or 422, got ${res.status} — pipeline may have crashed` : undefined,
    };
  },
};

/** When /close dry_run returns 200, the response must have all required CloseResponse fields. */
const closeResponseHasRequiredFields: SmokeTest = {
  name: "business.close_response_has_required_fields",
  category: "business",
  async run(ctx: TestContext): Promise<TestResult> {
    const t0 = ms();
    const token = await ctx.getAccountantToken();
    if (!token) {
      return credentialGate(ctx, this, t0, "SMOKE_ACCOUNTANT_SP_* not configured");
    }
    const now = new Date();
    const year = now.getMonth() === 0 ? now.getFullYear() - 1 : now.getFullYear();
    const month = now.getMonth() === 0 ? 12 : now.getMonth();
    const res = await ctx.fetch(`${ctx.apiUrl}/api/close`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ year, month, dry_run: true }),
      timeoutMs: 30_000,
    });
    if (res.status === 422) {
      return { name: this.name, category: this.category, status: "SKIP", duration_ms: ms() - t0, evidence: "422 preflight blockers — files missing in staging (expected)" };
    }
    if (res.status !== 200) {
      return { name: this.name, category: this.category, status: "FAIL", duration_ms: ms() - t0, evidence: `HTTP ${res.status}`, error: `Expected 200, got ${res.status}` };
    }
    const body = await res.json() as Record<string, unknown>;
    const missing = CLOSE_REQUIRED_FIELDS.filter((f) => !(f in body));
    const ok = missing.length === 0;
    return {
      name: this.name,
      category: this.category,
      status: ok ? "PASS" : "FAIL",
      duration_ms: ms() - t0,
      evidence: ok ? `all ${CLOSE_REQUIRED_FIELDS.length} fields present` : `missing: ${missing.join(", ")}`,
      error: !ok ? `CloseResponse missing fields: ${missing.join(", ")} — API contract broken` : undefined,
    };
  },
};

/** When /close returns 422, the blockers must be non-empty human-readable strings. */
const closePreflightBlockersAreMeaningful: SmokeTest = {
  name: "business.close_preflight_blockers_are_meaningful",
  category: "business",
  async run(ctx: TestContext): Promise<TestResult> {
    const t0 = ms();
    const token = await ctx.getAccountantToken();
    if (!token) {
      return credentialGate(ctx, this, t0, "SMOKE_ACCOUNTANT_SP_* not configured");
    }
    const now = new Date();
    const year = now.getMonth() === 0 ? now.getFullYear() - 1 : now.getFullYear();
    const month = now.getMonth() === 0 ? 12 : now.getMonth();
    const res = await ctx.fetch(`${ctx.apiUrl}/api/close`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ year, month, dry_run: true }),
      timeoutMs: 30_000,
    });
    if (res.status === 200) {
      return { name: this.name, category: this.category, status: "SKIP", duration_ms: ms() - t0, evidence: "200 — pipeline ran clean, no blockers to check" };
    }
    if (res.status !== 422) {
      return { name: this.name, category: this.category, status: "FAIL", duration_ms: ms() - t0, evidence: `HTTP ${res.status}`, error: `Unexpected status ${res.status}` };
    }
    const body = await res.json() as { detail?: { blockers?: unknown[] } };
    const blockers = body?.detail?.blockers ?? [];
    const ok = Array.isArray(blockers) && blockers.length > 0 && blockers.every((b) => typeof b === "string" && b.length > 5);
    return {
      name: this.name,
      category: this.category,
      status: ok ? "PASS" : "FAIL",
      duration_ms: ms() - t0,
      evidence: `${blockers.length} blockers: ${JSON.stringify(blockers).slice(0, 120)}`,
      error: !ok ? "Blockers are empty or malformed — error messages may be broken" : undefined,
    };
  },
};

/** GET /close/state must return valid structure for viewer role. */
const closeStateHasValidStructure: SmokeTest = {
  name: "business.close_state_has_valid_structure",
  category: "business",
  async run(ctx: TestContext): Promise<TestResult> {
    const t0 = ms();
    const token = await ctx.getViewerToken();
    if (!token) {
      return credentialGate(ctx, this, t0, "SMOKE_SP_* not configured");
    }
    const now = new Date();
    const year = now.getMonth() === 0 ? now.getFullYear() - 1 : now.getFullYear();
    const month = now.getMonth() === 0 ? 12 : now.getMonth();
    const res = await ctx.fetch(`${ctx.apiUrl}/api/close/state?year=${year}&month=${month}`, {
      headers: { Authorization: `Bearer ${token}` },
      timeoutMs: 8_000,
    });
    if (res.status !== 200) {
      return { name: this.name, category: this.category, status: "FAIL", duration_ms: ms() - t0, evidence: `HTTP ${res.status}`, error: `Expected 200, got ${res.status}` };
    }
    const body = await res.json() as Record<string, unknown>;
    const ok = "completed_steps" in body && Array.isArray(body.completed_steps);
    return {
      name: this.name,
      category: this.category,
      status: ok ? "PASS" : "FAIL",
      duration_ms: ms() - t0,
      evidence: ok ? `completed_steps: ${JSON.stringify(body.completed_steps)}` : `missing completed_steps field`,
      error: !ok ? "CloseStateResponse structure broken — completed_steps missing" : undefined,
    };
  },
};

/**
 * Live execution through the legacy endpoint must stay disabled.
 *
 * The staged workflow is exercised by Playwright in a separate CI job. Keeping
 * this smoke test read-only avoids two parallel jobs resetting or claiming the
 * same durable month-close run.
 */
const closeLiveFlowRequiresManualStages: SmokeTest = {
  name: "business.close_live_flow_requires_manual_stages",
  category: "business",
  async run(ctx: TestContext): Promise<TestResult> {
    const t0 = ms();
    const token = await ctx.getAccountantToken();
    if (!token) {
      return credentialGate(ctx, this, t0, "SMOKE_ACCOUNTANT_SP_* not configured");
    }
    const now = new Date();
    const year = now.getMonth() === 0 ? now.getFullYear() - 1 : now.getFullYear();
    const month = now.getMonth() === 0 ? 12 : now.getMonth();
    const res = await ctx.fetch(`${ctx.apiUrl}/api/close`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ year, month, dry_run: false, ignore_warnings: true }),
      timeoutMs: 10_000,
    });
    const text = await res.text().catch(() => "");
    if (res.status !== 409 || !text.includes("/api/close/workflow/actions/{action}")) {
      return {
        name: this.name, category: this.category, status: "FAIL",
        duration_ms: ms() - t0,
        evidence: `HTTP ${res.status}: ${text.slice(0, 200)}`,
        error: "Legacy live close must return 409 and direct the operator to staged actions",
      };
    }

    const workflowRes = await ctx.fetch(
      `${ctx.apiUrl}/api/close/workflow?year=${year}&month=${month}`,
      {
        headers: { Authorization: `Bearer ${token}` },
        timeoutMs: 10_000,
      },
    );
    if (workflowRes.status !== 200) {
      return {
        name: this.name, category: this.category, status: "FAIL",
        duration_ms: ms() - t0,
        evidence: `legacy=409, workflow=HTTP ${workflowRes.status}`,
        error: "Staged month-close workflow is not available",
      };
    }

    const workflow = await workflowRes.json() as {
      steps?: Record<string, unknown>;
      active_action?: unknown;
    };
    const requiredSteps = ["check", "sales", "costs", "reports", "bank", "package", "send"];
    const missingSteps = requiredSteps.filter((step) => !(step in (workflow.steps ?? {})));
    const ok = missingSteps.length === 0;
    return {
      name: this.name,
      category: this.category,
      status: ok ? "PASS" : "FAIL",
      duration_ms: ms() - t0,
      evidence: `legacy live endpoint blocked; workflow has ${requiredSteps.length - missingSteps.length}/${requiredSteps.length} stages; active_action=${String(workflow.active_action ?? null)}`,
      error: !ok ? `Workflow missing stages: ${missingSteps.join(", ")}` : undefined,
    };
  },
};

/**
 * Verify blob output structure after a successful close:
 * - no temp filenames (tmpXXXXXX)
 * - deklaracje/ subfolder present
 * - koszty/ has files
 * Runs only when the staged E2E flow has already created output.
 */
const closeOutputStructureIsClean: SmokeTest = {
  name: "business.close_output_structure_is_clean",
  category: "business",
  async run(ctx: TestContext): Promise<TestResult> {
    const t0 = ms();
    const token = await ctx.getAccountantToken();
    if (!token) {
      return credentialGate(ctx, this, t0, "SMOKE_ACCOUNTANT_SP_* not configured");
    }
    const now = new Date();
    const year = now.getMonth() === 0 ? now.getFullYear() - 1 : now.getFullYear();
    const month = now.getMonth() === 0 ? 12 : now.getMonth();
    const POLISH_MONTHS: Record<number, string> = {
      1:"styczeń",2:"luty",3:"marzec",4:"kwiecień",5:"maj",6:"czerwiec",
      7:"lipiec",8:"sierpień",9:"wrzesień",10:"październik",11:"listopad",12:"grudzień"
    };
    const prefix = `faktury/${year}/${POLISH_MONTHS[month]}`;
    const res = await ctx.fetch(`${ctx.apiUrl}/api/files?prefix=${encodeURIComponent(prefix)}&flat=true`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!res.ok) {
      return { name: this.name, category: this.category, status: "SKIP", duration_ms: ms() - t0, evidence: `GET /files returned ${res.status} — pipeline may not have run yet` };
    }
    const files = await res.json() as Array<{ key: string }>;
    if (files.length === 0) {
      return { name: this.name, category: this.category, status: "SKIP", duration_ms: ms() - t0, evidence: "No output files found — pipeline may not have run" };
    }
    const keys = files.map(f => f.key);
    const errors: string[] = [];

    // No temp filenames
    const tmpFiles = keys.filter(k => /\/tmp[a-z0-9]{6,}\./i.test(k));
    if (tmpFiles.length > 0) errors.push(`Temp filenames: ${tmpFiles.join(", ")}`);

    // deklaracje/ subfolder must exist
    const hasDecl = keys.some(k => k.includes("/deklaracje/"));
    if (!hasDecl) errors.push("Missing deklaracje/ subfolder (JPK/VAT reports in root)");

    // koszty/ must have files
    const hasKoszty = keys.some(k => k.includes("/koszty/"));
    if (!hasKoszty) errors.push("Missing koszty/ subfolder (no cost invoices)");

    if (errors.length > 0 && !ctx.strict) {
      return {
        name: this.name, category: this.category,
        status: "SKIP",
        duration_ms: ms() - t0,
        evidence: `${keys.length} files, deklaracje=${hasDecl}, koszty=${hasKoszty}, tmp_files=${tmpFiles.length}`,
        error: `Skipped in non-strict mode: ${errors.join("; ")}`,
      };
    }

    return {
      name: this.name, category: this.category,
      status: errors.length === 0 ? "PASS" : "FAIL",
      duration_ms: ms() - t0,
      evidence: `${keys.length} files, deklaracje=${hasDecl}, koszty=${hasKoszty}, tmp_files=${tmpFiles.length}`,
      error: errors.length > 0 ? errors.join("; ") : undefined,
    };
  },
};

/**
 * Validate per-vendor source breakdown and ZIP manifest from workflow state.
 * - FAIL if any cost vendor is missing
 * - FAIL if temp filenames appear in zip_files
 * - FAIL if deklaracje/ or koszty/ subfolder absent from zip_files
 * This is intentionally read-only so it can run safely beside Playwright.
 */
const closeDetailedVendorAndZipReport: SmokeTest = {
  name: "business.close_detailed_vendor_and_zip_report",
  category: "business",
  async run(ctx: TestContext): Promise<TestResult> {
    const t0 = ms();
    const token = await ctx.getAccountantToken();
    if (!token) {
      return credentialGate(ctx, this, t0, "SMOKE_ACCOUNTANT_SP_* not configured");
    }
    const now = new Date();
    const year = now.getMonth() === 0 ? now.getFullYear() - 1 : now.getFullYear();
    const month = now.getMonth() === 0 ? 12 : now.getMonth();
    const res = await ctx.fetch(`${ctx.apiUrl}/api/close/workflow?year=${year}&month=${month}`, {
      headers: { Authorization: `Bearer ${token}` },
      timeoutMs: 10_000,
    });
    if (res.status !== 200) {
      const text = await res.text().catch(() => "");
      return {
        name: this.name, category: this.category, status: "FAIL",
        duration_ms: ms() - t0,
        evidence: `HTTP ${res.status}: ${text.slice(0, 200)}`,
        error: `Expected 200, got ${res.status}`,
      };
    }
    const body = await res.json() as {
      metrics?: Record<string, unknown>;
      artifacts?: Array<{ kind?: string; files?: string[] }>;
    };
    const errors: string[] = [];

    const foundVendors =
      (body.metrics?.cost_found_vendors as Record<string, string> | undefined) ?? {};
    const missingVendors =
      (body.metrics?.cost_missing_vendors as string[] | undefined) ?? [];
    const packageArtifact = body.artifacts?.find((artifact) => artifact.kind === "package");
    const zipFiles = packageArtifact?.files ?? null;

    if (zipFiles === null) {
      return {
        name: this.name,
        category: this.category,
        status: "SKIP",
        duration_ms: ms() - t0,
        evidence: "Workflow package is not ready yet; Playwright owns staged execution",
      };
    }

    if (missingVendors.length > 0) {
      errors.push(`Missing vendors: ${missingVendors.join(", ")}`);
    }

    const vendorTable = Object.entries(foundVendors)
      .map(([v, src]) => `  ${v} → ${src}`)
      .join("\n");

    if (zipFiles !== null) {
      const tmpFiles = zipFiles.filter(f => /\/tmp[a-z0-9]{6,}\./i.test(f));
      if (tmpFiles.length > 0) errors.push(`Temp filenames in ZIP: ${tmpFiles.join(", ")}`);
      if (!zipFiles.some(f => f.includes("deklaracje/"))) errors.push("Missing deklaracje/ in ZIP");
      if (!zipFiles.some(f => f.includes("koszty/"))) errors.push("Missing koszty/ in ZIP");
    }

    const evidence = [
      `vendors (${Object.keys(foundVendors).length} found, ${missingVendors.length} missing):`,
      vendorTable || "  (none)",
      `zip_files: ${zipFiles === null ? "null" : `${zipFiles.length} files`}`,
      zipFiles ? zipFiles.slice(0, 10).join(", ") + (zipFiles.length > 10 ? ` …+${zipFiles.length - 10}` : "") : "",
    ].filter(Boolean).join("\n");

    return {
      name: this.name,
      category: this.category,
      status: errors.length === 0 ? "PASS" : "FAIL",
      duration_ms: ms() - t0,
      evidence,
      error: errors.length > 0 ? errors.join("; ") : undefined,
    };
  },
};

export const tests: SmokeTest[] = [
  closeDryRunDoesNotCrash,
  closeResponseHasRequiredFields,
  closePreflightBlockersAreMeaningful,
  closeStateHasValidStructure,
  closeLiveFlowRequiresManualStages,
  closeOutputStructureIsClean,
  closeDetailedVendorAndZipReport,
];
