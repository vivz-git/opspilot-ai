import { describe, expect, it } from "vitest";

import { formatDuration, formatTimestamp } from "@/lib/format";

describe("formatDuration", () => {
  it("renders an em dash for a run with no duration yet", () => {
    expect(formatDuration(null)).toBe("—");
    expect(formatDuration(undefined)).toBe("—");
  });

  it("renders sub-second durations in milliseconds", () => {
    expect(formatDuration(450)).toBe("450ms");
  });

  it("renders sub-minute durations in seconds", () => {
    expect(formatDuration(1_500)).toBe("1.5s");
  });

  it("renders sub-hour durations in minutes and seconds", () => {
    expect(formatDuration(65_000)).toBe("1m 5s");
  });

  it("renders long durations in hours and minutes", () => {
    expect(formatDuration(3_700_000)).toBe("1h 1m");
  });
});

describe("formatTimestamp", () => {
  it("formats a valid ISO timestamp", () => {
    const formatted = formatTimestamp("2026-09-19T10:00:00Z");
    expect(formatted).not.toBe("2026-09-19T10:00:00Z");
    expect(formatted.length).toBeGreaterThan(0);
  });

  it("falls back to the raw string for an unparseable value", () => {
    expect(formatTimestamp("not-a-date")).toBe("not-a-date");
  });
});
