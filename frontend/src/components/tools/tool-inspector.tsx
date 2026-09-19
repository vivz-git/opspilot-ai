"use client";

import { X } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { JsonBlock } from "@/components/ui/json-block";
import { Separator } from "@/components/ui/separator";
import type { ToolResource } from "@/lib/api/client";

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-muted-foreground">{label}</div>
      <div className="font-mono">{value}</div>
    </div>
  );
}

/**
 * The full contract for one registered tool (§8) — schema, policy metadata
 * and failure modes, exactly as `GET /tools` publishes it. Read-only: this
 * is a catalog, not a playground, so there is no way to invoke a tool from
 * here.
 */
export function ToolInspector({ tool, onClose }: { tool: ToolResource; onClose: () => void }) {
  return (
    <Card className="sticky top-4">
      <CardHeader className="flex-row items-start justify-between space-y-0">
        <div className="flex flex-col gap-1">
          <CardTitle className="font-mono text-sm">{tool.name}</CardTitle>
          <span className="font-mono text-xs text-muted-foreground">v{tool.version}</span>
        </div>
        <Button variant="ghost" size="icon" onClick={onClose} aria-label="Close tool inspector">
          <X className="h-4 w-4" />
        </Button>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <p className="text-sm text-muted-foreground">{tool.purpose}</p>

        <div className="grid grid-cols-2 gap-3 text-xs sm:grid-cols-3">
          <Fact label="Side effect" value={tool.side_effect} />
          <Fact label="Risk" value={tool.risk} />
          <Fact label="Verification" value={tool.verification} />
          <Fact label="Requires approval" value={tool.requires_approval ? "yes" : "no"} />
          <Fact label="Idempotent" value={tool.idempotent ? "yes" : "no"} />
          <Fact label="Timeout" value={`${tool.timeout_ms}ms`} />
          {tool.nondeterministic && <Fact label="Nondeterministic" value="yes" />}
          {tool.untrusted_output && <Fact label="Untrusted output" value="yes" />}
        </div>

        {tool.failure_modes.length > 0 && (
          <>
            <Separator />
            <div className="flex flex-col gap-2">
              <span className="text-xs font-medium text-muted-foreground">Failure modes</span>
              <ul className="flex flex-col gap-1.5">
                {tool.failure_modes.map((mode, i) => (
                  <li key={`${mode.error_class}-${i}`} className="flex items-start gap-2 text-xs">
                    <Badge variant="outline" className="shrink-0 font-mono text-[10px]">
                      {mode.error_class}
                    </Badge>
                    <span className="text-muted-foreground">{mode.description}</span>
                  </li>
                ))}
              </ul>
            </div>
          </>
        )}

        <Separator />

        <JsonBlock label="Input schema" value={tool.schemas.input} copyable />
        <JsonBlock label="Output schema" value={tool.schemas.output} copyable />
      </CardContent>
    </Card>
  );
}
