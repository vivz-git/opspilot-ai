"use client";

import * as React from "react";
import { useRouter } from "next/navigation";

import {
  EMPTY_RUN_FILTERS,
  RunFilters,
  hasActiveFilters,
  type RunFiltersValue,
} from "@/components/runs/run-filters";
import { RunsPagination } from "@/components/runs/runs-pagination";
import { RunsTable } from "@/components/runs/runs-table";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import type { RunListQuery, RunSummary } from "@/lib/api/client";
import { useRuns } from "@/lib/api/queries";
import { useDebouncedValue } from "@/lib/use-debounced-value";

const PAGE_SIZE = 25;

/** `datetime-local` input value -> the ISO string GET /runs expects, or undefined if unset. */
function toIsoOrUndefined(localValue: string): string | undefined {
  if (!localValue) return undefined;
  const date = new Date(localValue);
  return Number.isNaN(date.getTime()) ? undefined : date.toISOString();
}

export default function RunsPage() {
  const router = useRouter();
  const [filters, setFilters] = React.useState<RunFiltersValue>(EMPTY_RUN_FILTERS);
  // Cursors already handed back by the server, oldest first; the last entry
  // is the cursor for the page currently on screen (undefined = first page).
  // This is a client-side memo of opaque server cursors, never a computed
  // offset — "previous page" is just popping a cursor we were already given.
  const [cursorStack, setCursorStack] = React.useState<string[]>([]);

  const debouncedQ = useDebouncedValue(filters.q, 350);
  const debouncedParentRunId = useDebouncedValue(filters.parentRunId, 350);

  const query: RunListQuery = React.useMemo(() => {
    const q: RunListQuery = { limit: PAGE_SIZE };
    if (filters.status.length > 0) q.status = filters.status;
    if (debouncedQ.trim()) q.q = debouncedQ.trim();
    const since = toIsoOrUndefined(filters.since);
    if (since) q.since = since;
    const until = toIsoOrUndefined(filters.until);
    if (until) q.until = until;
    if (debouncedParentRunId.trim()) q.parent_run_id = debouncedParentRunId.trim();
    const cursor = cursorStack.at(-1);
    if (cursor) q.cursor = cursor;
    return q;
  }, [filters.status, debouncedQ, filters.since, filters.until, debouncedParentRunId, cursorStack]);

  const { data, isPending, isFetching, isError, error } = useRuns(query);

  function handleFiltersChange(next: RunFiltersValue) {
    setFilters(next);
    setCursorStack([]);
  }

  function handleNext() {
    if (data?.next_cursor) {
      setCursorStack((prev) => [...prev, data.next_cursor as string]);
    }
  }

  function handlePrevious() {
    setCursorStack((prev) => prev.slice(0, -1));
  }

  function handleSelectRun(run: RunSummary) {
    router.push(`/runs/${run.run_id}`);
  }

  const items = data?.items ?? [];

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-lg font-semibold">Runs</h1>
        <p className="text-sm text-muted-foreground">
          Every request submitted to the agent, as a planned, budgeted, terminating unit of work.
        </p>
      </div>

      <RunFilters value={filters} onChange={handleFiltersChange} />

      {isPending && (
        <div className="flex flex-col gap-2">
          <Skeleton className="h-9 w-full" />
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-10 w-full" />
        </div>
      )}

      {isError && (
        <Card>
          <CardHeader>
            <CardTitle>Could not load runs</CardTitle>
          </CardHeader>
          <CardContent className="text-sm text-muted-foreground">
            {error instanceof Error ? error.message : "Unknown error"}. Is the OpsPilot API
            running at the configured base URL?
          </CardContent>
        </Card>
      )}

      {!isPending && !isError && items.length === 0 && (
        <Card>
          <CardContent className="pt-6 text-sm text-muted-foreground">
            {hasActiveFilters(filters)
              ? "No runs match these filters."
              : "No runs yet. Submit a request through the OpsPilot API to see it here."}
          </CardContent>
        </Card>
      )}

      {!isPending && !isError && items.length > 0 && data && (
        <div className="flex flex-col gap-3">
          <Card className="overflow-hidden p-0">
            <RunsTable runs={items} onSelectRun={handleSelectRun} />
          </Card>
          <RunsPagination
            visibleCount={items.length}
            totalEstimate={data.total_estimate}
            hasPreviousPage={cursorStack.length > 0}
            hasNextPage={Boolean(data.next_cursor)}
            isFetching={isFetching}
            onPrevious={handlePrevious}
            onNext={handleNext}
          />
        </div>
      )}
    </div>
  );
}
