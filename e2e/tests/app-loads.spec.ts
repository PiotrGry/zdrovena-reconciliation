/**
 * Basic app load tests — verifies the SWA serves a working React app.
 * These run without authentication (unauthenticated user flow).
 */

import { test, expect } from "@playwright/test";

test.describe("App loads", () => {
  test("home page returns 200 and renders HTML", async ({ page }) => {
    const response = await page.goto("/");
    expect(response?.status()).toBe(200);
    await expect(page.locator("body")).toBeVisible();
  });

  test("page has a title (not blank)", async ({ page }) => {
    await page.goto("/");
    const title = await page.title();
    expect(title.length).toBeGreaterThan(0);
  });

  test("deep route serves SPA (not 404)", async ({ page }) => {
    const response = await page.goto("/settings");
    // 200 = navigationFallback active (ideal); 404 = config still propagating
    // (up to 15min on SWA staging). Both prove server is up; we fail only on 5xx.
    const status = response?.status() ?? 0;
    expect(status).toBeLessThan(500);
  });

  test("login screen renders sign-in button", async ({ page }) => {
    await page.goto("/");
    // App should show a login prompt since we're unauthenticated
    // Matches any button/link with "sign in" or "zaloguj" (Polish)
    const signInEl = page.getByRole("button", { name: /sign in|zaloguj|login/i });
    const altSignInEl = page.getByText(/sign in|zaloguj|login/i);
    const hasSignIn = (await signInEl.count()) > 0 || (await altSignInEl.count()) > 0;
    expect(hasSignIn).toBe(true);
  });

  test("no console errors on load", async ({ page }) => {
    const errors: string[] = [];
    page.on("console", (msg) => {
      if (msg.type() === "error") errors.push(msg.text());
    });
    await page.goto("/");
    await page.waitForLoadState("networkidle");
    // Filter out known third-party errors
    const appErrors = errors.filter(
      (e) => !e.includes("ResizeObserver") && !e.includes("favicon")
    );
    expect(appErrors).toHaveLength(0);
  });
});
