import { expect, test, type Page } from "@playwright/test";

/**
 * FE-004's key interaction path: open the queue, drill into one approval,
 * see the full authorized payload, and decide it. Mocks the API at the
 * network boundary (no live backend in this environment) — the point of
 * this spec is the frontend's request/response wiring and rendered states,
 * not re-verifying backend behavior already covered by its own test suite.
 */
const API_BASE = "http://127.0.0.1:9999";

const APPROVAL_ID = "a1b2c3d4-1111-4a2b-8c3d-4e5f6a7b8c9d";

function pendingApproval() {
  return {
    approval_id: APPROVAL_ID,
    run_id: "8f1e2c3a-6b4d-4e2f-9a1b-7c8d9e0f1a2b",
    step_id: "s4",
    tool: "send_email_mock",
    risk: "high",
    title: "Send outreach email to jane@acmecorp.com",
    summary: "Sends the drafted outreach email to the highest-scoring lead.",
    status: "pending",
    args_hash: "sha256:3f9a1c2b4d5e6f708192a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2c3d4",
    payload_preview: {
      to_email: "jane@acmecorp.com",
      draft_id: "draft-771",
      lead_id: "lead-4471",
      subject: "Quick question about your fintech stack",
      body: "Hi Jane,\n\nWorth a quick call this week?\n\nBest,\nOpsPilot",
      args: { to: "jane@acmecorp.com", draft_id: "draft-771" },
    },
    created_at: "2026-09-19T09:40:00Z",
    requested_at: "2026-09-19T09:40:01Z",
    expires_at: "2026-09-19T09:55:00Z",
    decided_at: null,
    decided_by: null,
    reason: null,
  };
}

function decidedApproval(status: "approved" | "rejected", reason: string | null) {
  return {
    ...pendingApproval(),
    status,
    decided_at: "2026-09-19T09:42:00Z",
    decided_by: null,
    reason,
  };
}

async function mockApprovalApi(page: Page) {
  let decided: ReturnType<typeof decidedApproval> | null = null;

  await page.route(`${API_BASE}/approvals/queue`, async (route) => {
    await route.fulfill({ json: [pendingApproval()] });
  });

  await page.route(`${API_BASE}/approvals/${APPROVAL_ID}`, async (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    await route.fulfill({ json: decided ?? pendingApproval() });
  });

  await page.route(`${API_BASE}/approvals/${APPROVAL_ID}/decision`, async (route) => {
    const body = route.request().postDataJSON() as { decision: "approve" | "reject"; reason?: string };
    decided = decidedApproval(body.decision === "approve" ? "approved" : "rejected", body.reason ?? null);
    await route.fulfill({ json: decided });
  });
}

test("operator reviews the full payload and approves a pending approval", async ({ page }) => {
  await mockApprovalApi(page);

  await page.goto("/approvals");
  await expect(page.getByRole("heading", { name: "Approvals" })).toBeVisible();

  const row = page.getByRole("link", { name: /Open approval/ });
  await expect(row).toBeVisible();
  await row.click();

  await expect(page).toHaveURL(new RegExp(`/approvals/${APPROVAL_ID}$`));
  await expect(page.getByText("Send outreach email to jane@acmecorp.com")).toBeVisible();

  // The exact, de-referenced payload — not a summary.
  await expect(page.getByText("Quick question about your fintech stack")).toBeVisible();
  await expect(page.getByText(/Worth a quick call this week/)).toBeVisible();
  await expect(page.getByText(/"draft_id": "draft-771"/)).toBeVisible();

  await page.getByRole("button", { name: "Approve" }).click();

  await expect(
    page.getByText(/can no longer be decided from this screen/)
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "Approve" })).toHaveCount(0);
});

test("rejecting requires a reason before it can be submitted", async ({ page }) => {
  await mockApprovalApi(page);

  await page.goto(`/approvals/${APPROVAL_ID}`);
  await page.getByRole("button", { name: "Reject" }).click();
  await page.getByRole("button", { name: "Confirm reject" }).click();

  await expect(page.getByText("A reason is required to reject.")).toBeVisible();

  await page.getByLabel(/Rejection reason/).fill("Wrong recipient");
  await page.getByRole("button", { name: "Confirm reject" }).click();

  await expect(page.getByRole("button", { name: "Confirm reject" })).toHaveCount(0);
});
