export type TestStatus = "PASS" | "FAIL" | "SKIP";

export interface SmokeTest {
  name: string;
  category: "api" | "frontend" | "auth" | "business";
  run(ctx: TestContext): Promise<TestResult>;
}

export interface TestContext {
  apiUrl: string;
  swaUrl: string;
  azureTenantId: string;
  azureClientId: string;
  azureApiClientId: string;
  azureSubscriptionId: string;
  /** Smoke test SP credentials — used by auth-real tests to acquire a token
   *  with zdrovena-viewer role and exercise authenticated endpoints. */
  smokeSpClientId: string;
  smokeSpClientSecret: string;
  /** Accountant SP credentials — used by business tests to trigger POST /close.
   *  Set SMOKE_ACCOUNTANT_SP_CLIENT_ID + SMOKE_ACCOUNTANT_SP_CLIENT_SECRET in CI. */
  smokeAccountantSpClientId: string;
  smokeAccountantSpClientSecret: string;
  verbose: boolean;
  /** Strict mode (--strict / SMOKE_STRICT=true). When true, missing required
   *  credentials or an unacquirable token becomes FAIL instead of SKIP, so a
   *  run cannot stay green without proving authenticated staging flows work.
   *  Conditional branch skips (e.g. 422 vs 200 alternate paths) stay SKIP. */
  strict: boolean;
  /** Fetch with a timeout. Default 10s. */
  fetch(url: string, opts?: RequestInit & { timeoutMs?: number }): Promise<Response>;
  /** Lazy-acquired viewer access token, cached for the run.
   *  Returns null if SP creds aren't configured (tests skip themselves). */
  getViewerToken(): Promise<string | null>;
  /** Lazy-acquired accountant access token, cached for the run.
   *  Returns null if accountant SP creds aren't configured (tests skip themselves). */
  getAccountantToken(): Promise<string | null>;
}

export interface TestResult {
  name: string;
  category: string;
  status: TestStatus;
  duration_ms: number;
  evidence: string;
  error?: string;
}

export interface SmokeReport {
  timestamp: string;
  api_url: string;
  swa_url: string;
  total: number;
  passed: number;
  failed: number;
  skipped: number;
  duration_ms: number;
  tests: TestResult[];
}
