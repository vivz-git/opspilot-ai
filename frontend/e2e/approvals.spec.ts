import { expect, test } from "@playwright/test";

import { CORS_HEADERS, mockHealthz, mockJson } from "./api-mock";

const APPROVAL_ID = "a1b2c3d4-e5f6-4a1b-8c9d-0e1f2a3b4c5d";
const RUN_ID = "8f1e2c3a-6b4d-4e2f-9a1b-7c8d9e0f1a2b";

function approval(overrides: Record<string, unknown> = {}) {
  return {
    approval_id: APPROVAL_ID,
    run_id: RUN_ID,
    step_id: "s3",
    tool: "send_email_mock",
    risk: "high",
    title: "Send outreach email to jane@example.com",
    summary: "Sends the approved outreach email to the highest-scoring lead.",
    status: "pending",
    args_hash: "sha256:abc123",
    payload_preview: { to: "jane@example.com", subject: "Following up" },
    created_at: "2026-09-19T08:12:00Z",
    requested_at: "2026-09-19T08:12:00Z",
    expires_at: "2026-09-20T08:12:00Z",
    decided_at: null,
    decided_by: null,
    reason: null,
    ...overrides,
  };
}

test.beforeEach(async ({ page }) => {
  await mockHealthz(page);
});

test("operator opens a pending approval from the queue and approves it", async ({ page }) => {
  await mockJson(page, "/approvals/queue", [approval()]);
  await mockJson(page, `/approvals/${APPROVAL_ID}`, approval());

  await page.goto("/approvals");
  const row = page.getByRole("link", { name: /Review approval/ });
  await expect(row).toBeVisible();
  await row.click();

  await expect(page).toHaveURL(`/approvals/${APPROVAL_ID}`);
  await expect(page.getByText("Send outreach email to jane@example.com")).toBeVisible();

  await page.route(
    (url) => url.pathname === `/approvals/${APPROVAL_ID}/decision`,
    (route) =>
      route.fulfill({
        headers: CORS_HEADERS,
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(approval({ status: "approved", decided_at: "2026-09-19T09:00:00Z" })),
      })
  );
  await page.getByRole("button", { name: "Approve" }).click();

  await expect(page.getByRole("button", { name: "Approve" })).toHaveCount(0);
});

test("operator rejects an approval with a required reason", async ({ page }) => {
  await mockJson(page, `/approvals/${APPROVAL_ID}`, approval());

  await page.goto(`/approvals/${APPROVAL_ID}`);
  await page.getByRole("button", { name: "Reject" }).click();

  const confirm = page.getByRole("button", { name: "Confirm reject" });
  await expect(confirm).toBeDisabled();

  await page.getByLabel(/Reason for rejecting/).fill("Wrong recipient domain");
  await expect(confirm).toBeEnabled();

  let requestBody: unknown;
  await page.route(
    (url) => url.pathname === `/approvals/${APPROVAL_ID}/decision`,
    (route) => {
      requestBody = route.request().postDataJSON();
      route.fulfill({
        headers: CORS_HEADERS,
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(
          approval({ status: "rejected", reason: "Wrong recipient domain", decided_at: "2026-09-19T09:00:00Z" })
        ),
      });
    }
  );
  await confirm.click();

  await expect(page.getByRole("button", { name: "Reject" })).toHaveCount(0);
  expect(requestBody).toMatchObject({ decision: "reject", reason: "Wrong recipient domain" });
});

for (const [code, expectedText] of [
  ["approval_not_pending", "no longer pending"],
  ["approval_expired", "expired before a decision"],
  ["approval_superseded", "superseded"],
  ["run_not_resumable", "no longer resumable"],
] as const) {
  test(`shows a specific message for ${code}`, async ({ page }) => {
    await mockJson(page, `/approvals/${APPROVAL_ID}`, approval());
    await page.route(
      (url) => url.pathname === `/approvals/${APPROVAL_ID}/decision`,
      (route) =>
        route.fulfill({
          headers: CORS_HEADERS,
          status: 409,
          contentType: "application/problem+json",
          body: JSON.stringify({ code, detail: "server detail text", status: 409 }),
        })
    );

    await page.goto(`/approvals/${APPROVAL_ID}`);
    await page.getByRole("button", { name: "Approve" }).click();

    await expect(page.getByText(new RegExp(expectedText, "i"))).toBeVisible();
    // Approve remains available — the decision was refused, not applied.
    await expect(page.getByRole("button", { name: "Approve" })).toBeVisible();
  });
}

test("approvals detail shows a not-found state for an unknown id", async ({ page }) => {
  await mockJson(page, "/approvals/does-not-exist", { code: "not_found", detail: "gone" }, 404);

  await page.goto("/approvals/does-not-exist");
  await expect(page.getByText("Approval not found")).toBeVisible();
});
