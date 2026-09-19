import { expect, test } from "@playwright/test";

import { mockHealthz, mockJson } from "./api-mock";

const TOOLS = [
  {
    name: "search_leads",
    version: "1.0.0",
    purpose: "Find candidate leads matching business filters.",
    side_effect: "read_only",
    requires_approval: false,
    risk: "low",
    verification: "invariant",
    idempotent: true,
    nondeterministic: false,
    untrusted_output: false,
    timeout_ms: 10000,
    failure_modes: [
      { error_class: "input_validation", description: "arguments failed the input schema" },
    ],
    schemas: {
      input: { type: "object", properties: { query: { type: "string" } } },
      output: { type: "object", properties: { leads: { type: "array" } } },
    },
  },
  {
    name: "send_email_mock",
    version: "1.0.0",
    purpose: "Record an outbound email in the mock outbox. Never sends real mail.",
    side_effect: "outbound",
    requires_approval: true,
    risk: "high",
    verification: "readback",
    idempotent: true,
    nondeterministic: false,
    untrusted_output: false,
    timeout_ms: 10000,
    failure_modes: [],
    schemas: {
      input: { type: "object", properties: { to: { type: "string" } } },
      output: { type: "object", properties: { message_id: { type: "string" } } },
    },
  },
];

test.beforeEach(async ({ page }) => {
  await mockHealthz(page);
});

test("operator browses the tool catalog and inspects a gated tool's schema", async ({ page }) => {
  await mockJson(page, "/tools", TOOLS);

  await page.goto("/tools");
  await expect(page.getByRole("heading", { name: "Tools" })).toBeVisible();

  const gatedRow = page.getByRole("link", { name: "Inspect tool send_email_mock" });
  await expect(gatedRow).toBeVisible();
  await expect(gatedRow.getByText("gated")).toBeVisible();

  await gatedRow.click();
  await expect(page.getByText("Input schema", { exact: true })).toBeVisible();
  await expect(page.getByText("Output schema", { exact: true })).toBeVisible();
  await expect(page.getByText(/"to"/)).toBeVisible();

  // Read-only catalog: no execute/run affordance anywhere on the page.
  await expect(page.getByRole("button", { name: /run|execute|invoke/i })).toHaveCount(0);
});

test("tools page shows an empty state when the registry is empty", async ({ page }) => {
  await mockJson(page, "/tools", []);

  await page.goto("/tools");
  await expect(page.getByText("No tools are registered.")).toBeVisible();
});
