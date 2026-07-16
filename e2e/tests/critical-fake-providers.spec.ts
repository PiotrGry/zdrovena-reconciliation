import crypto from "node:crypto";
import { expect, request as playwrightRequest, test, type APIRequestContext, type Page } from "@playwright/test";

const API_URL = (process.env.API_URL ?? "http://localhost:8000").replace(/\/$/, "");
const FAKE_PROVIDER_URL = (process.env.FAKE_PROVIDER_URL ?? "http://localhost:9009").replace(/\/$/, "");
const TOKEN = process.env.AZURE_TEST_BEARER_TOKEN ?? "dev-token";
const WEBHOOK_SECRET = process.env.SHOPIFY_WEBHOOK_SECRET ?? "e2e-webhook-secret";

test.describe("critical fake-provider shipping flows", () => {
  test.skip(
    process.env.RUN_FAKE_PROVIDER_E2E !== "1",
    "Set RUN_FAKE_PROVIDER_E2E=1 with local fake providers and VITE_AUTH_DISABLED=true",
  );

  let api: APIRequestContext;
  let fake: APIRequestContext;

  test.beforeAll(async () => {
    api = await playwrightRequest.newContext({
      extraHTTPHeaders: { Authorization: `Bearer ${TOKEN}` },
    });
    fake = await playwrightRequest.newContext();
  });

  test.afterAll(async () => {
    await api?.dispose();
    await fake?.dispose();
  });

  test.beforeEach(async () => {
    await resetState(api, fake);
  });

  test.afterEach(async () => {
    await resetState(api, fake);
  });

  test("webhook creates a draft visible in the shipping UI", async ({ page }) => {
    const order = shopifyOrder({ orderNumber: 990001, shippingTitle: "InPost Paczkomat" });
    await sendShopifyWebhook(api, order, "e2e-webhook-draft");

    const draft = await waitForDraft(api, "990001");
    expect(draft.status).toBe("pending");

    await openAppPage(page, "shipping");
    const row = page.getByTestId(`shipping-row-${draft.id}`);
    await expect(row).toContainText("#990001");
    await expect(row).toContainText("E2E Webhook");
  });

  test("executes an InPost draft through the UI and does not create pickup", async ({ page }) => {
    const order = shopifyOrder({ orderNumber: 990002, shippingTitle: "InPost Paczkomat" });
    await sendShopifyWebhook(api, order, "e2e-webhook-execute");
    const draft = await waitForDraft(api, "990002");

    await openAppPage(page, "shipping");
    await page.getByTestId(`shipping-expand-${draft.id}`).click();
    await page.getByTestId(`shipping-execute-${draft.id}`).click();
    await page.getByRole("button", { name: "Potwierdź" }).click();

    const executed = await waitForDraft(api, "990002", (d) => d.status === "created");
    expect(executed.tracking_number).toMatch(/^620/);
    expect(executed.pickup_ordered).toBe(false);

    await page.getByTestId(`shipping-expand-${draft.id}`).click();
    await expect(page.getByTestId(`shipping-row-${draft.id}`)).toContainText(executed.tracking_number);

    const state = await fakeJson(fake, "/__fake__/state");
    expect(Object.keys(state.inpost.shipments)).toHaveLength(1);
    expect(Object.keys(state.inpost.dispatches)).toHaveLength(0);
  });

  test("provider 500 shows Polish toast with correlation id", async ({ page }) => {
    const draft = await seedDraft(api, {
      id: "e2e-provider-500",
      shopify_order_number: "990003",
      customer_name: "E2E Provider Error",
      courier: "inpost",
      service: "inpost_locker_standard",
      status: "pending",
    });
    await fake.post(`${FAKE_PROVIDER_URL}/__fake__/scenario`, {
      data: { provider: "inpost", operation: "create_shipment", mode: "server_error" },
    });

    await openAppPage(page, "shipping");
    await page.getByTestId(`shipping-expand-${draft.id}`).click();
    await page.getByTestId(`shipping-execute-${draft.id}`).click();
    await page.getByRole("button", { name: "Potwierdź" }).click();

    const toast = page.locator(".toast-msg").last();
    await expect(toast).toContainText("Nie udało się zrealizować przesyłki");
    await expect(toast).toContainText(/Chwilowy problem z przewoźnikiem|Błąd komunikacji/);
    await expect(toast).toContainText(/\(ID: [^)]+\)/);
  });

  test("invoice preview amount matches created fake-provider invoice", async ({ page }) => {
    const draft = await seedDraft(api, {
      id: "e2e-invoice-parity",
      source: "allegro",
      external_order_id: "fake-order-1",
      shopify_order_number: "fake-order-1",
      customer_name: "Fake Buyer",
      courier: "allegro_delivery",
      service: "allegro_delivery",
      status: "pending",
    });

    await openAppPage(page, "shipping");
    await page.getByTestId(`shipping-expand-${draft.id}`).click();
    await page.getByTestId(`shipping-invoice-${draft.id}`).click();

    await expect(page.getByRole("cell", { name: "Do zapłaty" })).toBeVisible();
    await expect(page.getByRole("cell", { name: "29.99 zł" }).first()).toBeVisible();
    await expect(page.getByText(/Zgadza się z Allegro/)).toBeVisible();
    await page.getByRole("button", { name: /Utwórz i załącz do Allegro/ }).click();

    const updated = await waitForDraft(api, "fake-order-1", (d) => Boolean(d.fakturownia_invoice_id));
    expect(updated.fakturownia_invoice_id).toBeTruthy();

    const state = await fakeJson(fake, "/__fake__/state");
    const createdInvoice = Object.values(state.fakturownia.invoices)[0] as {
      positions?: Array<{ total_price_gross?: number }>;
    };
    expect(createdInvoice.positions?.[0]?.total_price_gross).toBe(29.99);
  });

  test("DLQ retry creates a draft visible in the shipping UI", async ({ page }) => {
    await api.post(`${API_URL}/api/__test__/shipping/dlq`, {
      data: {
        id: "e2e-dlq-retry",
        source: "shopify",
        error: "RuntimeError: transient storage failure",
        payload: shopifyOrder({ orderNumber: 990005, shippingTitle: "InPost Paczkomat" }),
      },
    });

    await openAppPage(page, "dlq");
    await expect(page.getByTestId("dlq-row-e2e-dlq-retry")).toContainText("990005");
    await page.getByTestId("dlq-retry-e2e-dlq-retry").click();
    await expect(page.locator(".toast-msg").last()).toContainText("Wpis DLQ ponowiony");

    const draft = await waitForDraft(api, "990005");
    await openAppPage(page, "shipping");
    await expect(page.getByTestId(`shipping-row-${draft.id}`)).toContainText("#990005");
  });
});

async function resetState(api: APIRequestContext, fake: APIRequestContext) {
  await fake.post(`${FAKE_PROVIDER_URL}/__fake__/reset`).catch(() => undefined);
  await api.post(`${API_URL}/api/__test__/shipping/reset`).catch(() => undefined);
}

async function openAppPage(page: Page, appPage: "shipping" | "dlq") {
  await page.addInitScript((targetPage) => {
    window.localStorage.setItem("zdrovena_page", targetPage);
    window.localStorage.setItem("zdrovena_lang", "pl");
  }, appPage);
  await page.goto("/");
  await expect(page.getByRole("main")).toBeVisible();
}

async function sendShopifyWebhook(api: APIRequestContext, order: Record<string, unknown>, id: string) {
  const body = JSON.stringify(order);
  const signature = crypto.createHmac("sha256", WEBHOOK_SECRET).update(body).digest("base64");
  const webhookId = `${id}-${Date.now()}-${crypto.randomUUID()}`;
  const res = await api.post(`${API_URL}/api/webhooks/shopify/order-create`, {
    data: body,
    headers: {
      "Content-Type": "application/json",
      "X-Shopify-Hmac-Sha256": signature,
      "X-Shopify-Topic": "orders/create",
      "X-Shopify-Shop-Domain": "e2e.myshopify.com",
      "X-Shopify-Webhook-Id": webhookId,
    },
  });
  expect(res.ok()).toBeTruthy();
}

async function waitForDraft(
  api: APIRequestContext,
  orderNumber: string,
  predicate: (draft: Record<string, any>) => boolean = () => true,
) {
  const deadline = Date.now() + 15_000;
  while (Date.now() < deadline) {
    const res = await api.get(`${API_URL}/api/shipping/drafts`);
    if (res.ok()) {
      const body = await res.json();
      const draft = (body.drafts ?? []).find(
        (candidate: Record<string, any>) =>
          String(candidate.shopify_order_number) === orderNumber && predicate(candidate),
      );
      if (draft) return draft as Record<string, any>;
    }
    await new Promise((resolve) => setTimeout(resolve, 300));
  }
  throw new Error(`Timed out waiting for draft ${orderNumber}`);
}

async function seedDraft(api: APIRequestContext, overrides: Record<string, unknown>) {
  const draft = {
    id: "e2e-draft",
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    order_date: new Date().toISOString(),
    source: "shopify",
    external_order_id: "990000",
    shopify_order_id: "990000",
    shopify_order_number: "990000",
    customer_name: "E2E Customer",
    courier: "inpost",
    service: "inpost_locker_standard",
    apaczka_service_id: null,
    tracking_number: null,
    tracking_company: null,
    courier_draft_id: null,
    dispatch_order_id: null,
    status: "pending",
    packages_count: 1,
    packages_breakdown: [{ type: "1-pak", qty: 1 }],
    total_qty: 1,
    order_items: [{ name: "HUMIO", quantity: 1 }],
    pickup_ordered: false,
    receiver: {
      first_name: "E2E",
      last_name: "Customer",
      email: "e2e@example.test",
      phone: "+48600111222",
      locker_id: "WAW01A",
    },
    shipping_address: {
      street: "Prosta",
      building_number: "1",
      flat_number: "",
      city: "Warszawa",
      post_code: "00-001",
    },
    parcel: { template: "large", weight_kg: null },
    error: null,
    fulfillment_status: "unfulfilled",
    fakturownia_invoice_id: null,
    ...overrides,
  };
  const res = await api.post(`${API_URL}/api/__test__/shipping/drafts`, { data: draft });
  expect(res.ok()).toBeTruthy();
  return await res.json();
}

async function fakeJson(fake: APIRequestContext, path: string) {
  const res = await fake.get(`${FAKE_PROVIDER_URL}${path}`);
  expect(res.ok()).toBeTruthy();
  return await res.json();
}

function shopifyOrder({
  orderNumber,
  shippingTitle,
}: {
  orderNumber: number;
  shippingTitle: string;
}) {
  return {
    id: Number(`5000${orderNumber}`),
    order_number: orderNumber,
    name: `#${orderNumber}`,
    created_at: new Date().toISOString(),
    financial_status: "paid",
    fulfillment_status: null,
    shipping_lines: [{ title: shippingTitle }],
    note_attributes: [{ name: "PickupPointId", value: "WAW01A" }],
    shipping_address: {
      first_name: "E2E",
      last_name: "Webhook",
      address1: "Prosta 1",
      city: "Warszawa",
      zip: "00-001",
      country_code: "PL",
      phone: "+48600111222",
    },
    customer: {
      first_name: "E2E",
      last_name: "Webhook",
      email: "e2e@example.test",
      phone: "+48600111222",
    },
    line_items: [{ name: "HUMIO", title: "HUMIO", quantity: 1, sku: "HUMIO" }],
  };
}
