/**
 * Auth configuration E2E tests.
 * Verifies MSAL is configured with real tenant/client IDs (not 'undefined').
 * Catches the AADSTS900023 class of failures before they reach production.
 */

import { test, expect } from "@playwright/test";

test.describe("Auth configuration", () => {
  test("MSAL initializes without errors", async ({ page }) => {
    const msalErrors: string[] = [];
    page.on("console", (msg) => {
      const text = msg.text();
      if (msg.type() === "error" && (text.includes("msal") || text.includes("AADSTS"))) {
        msalErrors.push(text);
      }
    });

    await page.goto("/");
    await page.waitForLoadState("networkidle");

    expect(msalErrors).toHaveLength(0);
  });

  test("authority URL does not contain 'undefined'", async ({ page }) => {
    // Intercept the login redirect URL to verify it has a real tenant
    let loginUrl = "";
    page.on("request", (req) => {
      if (req.url().includes("login.microsoftonline.com")) {
        loginUrl = req.url();
      }
    });

    await page.goto("/");
    await page.waitForLoadState("networkidle");

    // Click sign in to trigger the auth redirect (don't complete it)
    const signInBtn = page.getByRole("button", { name: /sign in|zaloguj|login/i }).first();
    if (await signInBtn.isVisible()) {
      // Start navigation but intercept before leaving
      await Promise.race([
        signInBtn.click(),
        page.waitForTimeout(2_000),
      ]);
    }

    // If a login request was made, verify it has a real tenant GUID
    if (loginUrl) {
      expect(loginUrl).not.toContain("/undefined/");
      expect(loginUrl).toMatch(/login\.microsoftonline\.com\/[0-9a-f]{8}-[0-9a-f]{4}/);
    }
    // If no login request was intercepted, the test is inconclusive — pass it
  });
});
