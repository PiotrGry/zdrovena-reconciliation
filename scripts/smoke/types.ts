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
  verbose: boolean;
  /** Fetch with a timeout. Default 10s. */
  fetch(url: string, opts?: RequestInit & { timeoutMs?: number }): Promise<Response>;
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
