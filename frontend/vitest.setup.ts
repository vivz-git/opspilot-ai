import "@testing-library/jest-dom/vitest";

/**
 * jsdom has no `EventSource`. Most component tests never touch SSE at all —
 * they just render a page that happens to mount `useRunEvents` — so a inert
 * global stand-in keeps them from crashing. Tests that actually exercise the
 * live-update behavior (`use-run-events.test.ts`) inject their own fake via
 * `createSource` instead of relying on this one.
 */
if (typeof globalThis.EventSource === "undefined") {
  class InertEventSource extends EventTarget {
    static readonly CONNECTING = 0;
    static readonly OPEN = 1;
    static readonly CLOSED = 2;
    readonly CONNECTING = 0;
    readonly OPEN = 1;
    readonly CLOSED = 2;
    readyState = 0;
    onopen: (() => void) | null = null;
    onerror: (() => void) | null = null;
    onmessage: (() => void) | null = null;
    constructor(_url: string) {
      super();
    }
    close() {
      this.readyState = 2;
    }
  }
  // @ts-expect-error - test-only stand-in, not a spec-complete EventSource.
  globalThis.EventSource = InertEventSource;
}
