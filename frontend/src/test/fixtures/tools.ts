import type { ToolListResponse, ToolResource } from "@/lib/api/client";

/** Fixture data typed against the generated schema — see fixtures/runs.ts. */
export function makeTool(overrides: Partial<ToolResource> = {}): ToolResource {
  return {
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
    timeout_ms: 10_000,
    failure_modes: [
      { error_class: "input_validation", description: "arguments failed the input schema" },
      { error_class: "transient", description: "store or adapter temporarily unavailable" },
    ],
    schemas: {
      input: { type: "object", properties: { query: { type: "string" } } },
      output: { type: "object", properties: { leads: { type: "array" } } },
    },
    ...overrides,
  };
}

export const TOOLS_FIXTURE: ToolListResponse = [
  makeTool(),
  makeTool({
    name: "send_email_mock",
    purpose: "Record an outbound email in the mock outbox. Never sends real mail.",
    side_effect: "outbound",
    requires_approval: true,
    risk: "high",
    verification: "readback",
  }),
];
