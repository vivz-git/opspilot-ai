"use client";

import * as React from "react";
import { Play } from "lucide-react";

import { Button } from "@/components/ui/button";
import { ApiError } from "@/lib/api/client";
import { useCreateEvaluationRun } from "@/lib/api/queries";
import { REQUIRED_SUITE_NAMES } from "@/lib/evaluations";

/** Triggers a new suite run (POST /evaluations/runs) — the operator picks a
 * suite already declared in evals/suites.yaml; nothing here invents one. */
export function RunSuiteControl({ onStarted }: { onStarted?: (evaluationRunId: string) => void }) {
  const [suite, setSuite] = React.useState<string>("all");
  const mutation = useCreateEvaluationRun();

  function handleRun() {
    mutation.mutate(
      { suite },
      {
        onSuccess: (run) => onStarted?.(run.evaluation_run_id),
      }
    );
  }

  return (
    <div className="flex flex-wrap items-center gap-2">
      <label htmlFor="evaluation-suite-select" className="text-xs text-muted-foreground">
        Suite
      </label>
      <select
        id="evaluation-suite-select"
        value={suite}
        onChange={(e) => setSuite(e.target.value)}
        disabled={mutation.isPending}
        className="h-8 rounded-md border border-input bg-background px-2 text-sm"
      >
        {REQUIRED_SUITE_NAMES.map((name) => (
          <option key={name} value={name}>
            {name}
          </option>
        ))}
      </select>
      <Button size="sm" onClick={handleRun} disabled={mutation.isPending}>
        <Play className="h-3.5 w-3.5" aria-hidden />
        {mutation.isPending ? "Starting…" : "Run suite"}
      </Button>
      {mutation.isError && (
        <span className="text-xs text-destructive">
          {mutation.error instanceof ApiError
            ? mutation.error.body &&
              typeof mutation.error.body === "object" &&
              "detail" in mutation.error.body
              ? String((mutation.error.body as { detail?: unknown }).detail)
              : mutation.error.message
            : "Could not start the suite."}
        </span>
      )}
    </div>
  );
}
