"use client";

import * as React from "react";
import { SlidersHorizontal, X } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { RunStatus } from "@/lib/api/client";
import { cn } from "@/lib/utils";

/** All values of the backend's RunStatus enum — an existing constant for the filter UI to enumerate. */
export const RUN_STATUSES: RunStatus[] = [
  "created",
  "queued",
  "running",
  "awaiting_approval",
  "completed",
  "failed",
  "rejected",
  "cancelled",
  "expired",
];

export interface RunFiltersValue {
  status: RunStatus[];
  q: string;
  since: string;
  until: string;
  parentRunId: string;
}

export const EMPTY_RUN_FILTERS: RunFiltersValue = {
  status: [],
  q: "",
  since: "",
  until: "",
  parentRunId: "",
};

export function hasActiveFilters(value: RunFiltersValue): boolean {
  return (
    value.status.length > 0 ||
    value.q.trim() !== "" ||
    value.since !== "" ||
    value.until !== "" ||
    value.parentRunId.trim() !== ""
  );
}

export function RunFilters({
  value,
  onChange,
}: {
  value: RunFiltersValue;
  onChange: (next: RunFiltersValue) => void;
}) {
  const [advancedOpen, setAdvancedOpen] = React.useState(false);

  function toggleStatus(status: RunStatus) {
    const active = value.status.includes(status);
    onChange({
      ...value,
      status: active ? value.status.filter((s) => s !== status) : [...value.status, status],
    });
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <Input
          value={value.q}
          onChange={(e) => onChange({ ...value, q: e.target.value })}
          placeholder="Search request text…"
          aria-label="Search run request text"
          className="max-w-xs"
        />
        <Button
          type="button"
          variant={advancedOpen ? "secondary" : "outline"}
          size="sm"
          onClick={() => setAdvancedOpen((open) => !open)}
          aria-expanded={advancedOpen}
        >
          <SlidersHorizontal className="h-3.5 w-3.5" aria-hidden />
          Filters
        </Button>
        {hasActiveFilters(value) && (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => onChange(EMPTY_RUN_FILTERS)}
          >
            <X className="h-3.5 w-3.5" aria-hidden />
            Clear
          </Button>
        )}
      </div>

      <div className="flex flex-wrap gap-1.5" role="group" aria-label="Filter by status">
        {RUN_STATUSES.map((status) => {
          const active = value.status.includes(status);
          return (
            <button
              key={status}
              type="button"
              aria-pressed={active}
              onClick={() => toggleStatus(status)}
              className="focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring rounded-md"
            >
              <Badge
                variant={active ? "default" : "outline"}
                className={cn("cursor-pointer font-mono transition-colors", !active && "text-muted-foreground")}
              >
                {status}
              </Badge>
            </button>
          );
        })}
      </div>

      {advancedOpen && (
        <div className="grid grid-cols-1 gap-3 rounded-md border border-border/60 bg-card/40 p-3 sm:grid-cols-3">
          <label className="flex flex-col gap-1 text-xs text-muted-foreground">
            Since
            <Input
              type="datetime-local"
              value={value.since}
              onChange={(e) => onChange({ ...value, since: e.target.value })}
            />
          </label>
          <label className="flex flex-col gap-1 text-xs text-muted-foreground">
            Until
            <Input
              type="datetime-local"
              value={value.until}
              onChange={(e) => onChange({ ...value, until: e.target.value })}
            />
          </label>
          <label className="flex flex-col gap-1 text-xs text-muted-foreground">
            Parent run ID
            <Input
              value={value.parentRunId}
              onChange={(e) => onChange({ ...value, parentRunId: e.target.value })}
              placeholder="uuid"
              className="font-mono"
            />
          </label>
        </div>
      )}
    </div>
  );
}
