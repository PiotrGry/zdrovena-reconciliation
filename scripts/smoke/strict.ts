/**
 * Strict-mode helpers for the smoke runner.
 *
 * R4-A: a missing credential or an unacquirable token must NOT silently become
 * SKIP in strict mode — that is exactly the false-green failure this suite is
 * meant to catch. `credentialGate` centralises the decision so every test that
 * depends on a token/secret classifies the absence identically.
 */

import type { TestContext, TestResult, TestStatus } from "./types.js";

/**
 * Classify a missing-credential / unacquirable-token situation.
 * - non-strict → SKIP (optional locally, no creds configured)
 * - strict     → FAIL (release validation must prove authenticated flows work)
 */
export function credentialGate(
  ctx: TestContext,
  meta: { name: string; category: string },
  startMs: number,
  reason: string,
): TestResult {
  const status: TestStatus = ctx.strict ? "FAIL" : "SKIP";
  return {
    name: meta.name,
    category: meta.category,
    status,
    duration_ms: Date.now() - startMs,
    evidence: reason,
    error: ctx.strict
      ? `strict mode: required credential/token missing — ${reason}`
      : undefined,
  };
}
