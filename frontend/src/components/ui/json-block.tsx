"use client";

import * as React from "react";
import { Check, Copy } from "lucide-react";

import { Button } from "@/components/ui/button";

/**
 * A labeled, monospaced JSON payload — the one rendering used everywhere a
 * step, case or tool needs to show raw input/output/schema data (step
 * inspector, case inspector, tool catalog). `copyable` adds a copy-to-
 * clipboard affordance for larger blocks (schemas) without cluttering the
 * small ones (a single tool_call's input/output).
 */
export function JsonBlock({
  label,
  value,
  copyable = false,
}: {
  label: string;
  value: unknown;
  copyable?: boolean;
}) {
  const [copied, setCopied] = React.useState(false);

  if (value == null) return null;
  const text = JSON.stringify(value, null, 2);

  async function handleCopy() {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard access can be denied by the browser; the JSON is still
      // visible and selectable, so this is a silent no-op, not an error.
    }
  }

  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center justify-between">
        <span className="text-xs font-medium text-muted-foreground">{label}</span>
        {copyable && (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="h-6 gap-1 px-1.5 text-xs text-muted-foreground"
            onClick={handleCopy}
            aria-label={`Copy ${label} JSON`}
          >
            {copied ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
            {copied ? "Copied" : "Copy"}
          </Button>
        )}
      </div>
      <pre className="max-h-48 overflow-auto rounded-md border border-border/60 bg-background p-2 font-mono text-[11px] leading-relaxed">
        {text}
      </pre>
    </div>
  );
}
