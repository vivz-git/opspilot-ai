"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { Play } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { useCreateRun } from "@/lib/api/queries";
import { CANONICAL_REQUEST, MAX_REQUEST_LENGTH, describeRunSubmissionError } from "@/lib/runs";

/**
 * Submits a request as a new run (`POST /runs` with `auto_start`) and opens
 * the run the server created. Nothing about the run is decided here: the
 * backend validates the request, plans it, gates it and executes it, and the
 * detail page renders what the backend reports — this form only asks.
 */
export function NewRunForm() {
  const router = useRouter();
  const [userRequest, setUserRequest] = React.useState("");
  const create = useCreateRun();
  const pending = create.isPending;
  const canSubmit = userRequest.trim().length > 0 && !pending;

  function submit() {
    if (!canSubmit) return;
    create.mutate(
      { user_request: userRequest, auto_start: true },
      { onSuccess: (run) => router.push(`/runs/${run.run_id}`) }
    );
  }

  const error = create.isError ? describeRunSubmissionError(create.error) : null;

  return (
    <Card>
      <CardContent className="pt-6">
        <form
          aria-label="Submit a run"
          className="flex flex-col gap-3"
          onSubmit={(e) => {
            e.preventDefault();
            submit();
          }}
        >
          <div className="flex items-baseline justify-between gap-2">
            <label htmlFor="new-run-request" className="text-sm font-medium">
              New run
            </label>
            <button
              type="button"
              onClick={() => setUserRequest(CANONICAL_REQUEST)}
              disabled={pending}
              className="text-xs text-muted-foreground underline-offset-4 hover:text-foreground hover:underline disabled:opacity-50"
            >
              Use the canonical request
            </button>
          </div>

          <textarea
            id="new-run-request"
            value={userRequest}
            onChange={(e) => setUserRequest(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                e.preventDefault();
                submit();
              }
            }}
            rows={2}
            maxLength={MAX_REQUEST_LENGTH}
            disabled={pending}
            aria-describedby="new-run-hint"
            placeholder="Describe the outcome you want, e.g. find leads, research them, draft outreach…"
            className="w-full resize-y rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
          />

          {error && (
            <div
              role="alert"
              className="rounded-md border border-destructive/40 bg-destructive/10 p-2 text-xs text-destructive"
            >
              {error.message}
              {error.traceId && (
                <span className="mt-1 block font-mono text-muted-foreground">
                  trace_id {error.traceId}
                </span>
              )}
            </div>
          )}

          <div className="flex flex-wrap items-center justify-between gap-2">
            <p id="new-run-hint" className="text-xs text-muted-foreground">
              Starts immediately. Mutating and outbound steps pause for your approval before they
              run.{" "}
              <span className="font-mono">
                {userRequest.length}/{MAX_REQUEST_LENGTH}
              </span>
            </p>
            <Button type="submit" size="sm" disabled={!canSubmit} aria-busy={pending}>
              <Play className="h-3.5 w-3.5" aria-hidden />
              {pending ? "Submitting…" : "Submit run"}
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  );
}
