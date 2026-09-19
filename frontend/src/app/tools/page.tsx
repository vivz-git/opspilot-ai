"use client";

import * as React from "react";

import { ToolCatalogTable } from "@/components/tools/tool-catalog-table";
import { ToolInspector } from "@/components/tools/tool-inspector";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import type { ToolResource } from "@/lib/api/client";
import { useTools } from "@/lib/api/queries";

export default function ToolsPage() {
  const [selectedTool, setSelectedTool] = React.useState<string | null>(null);
  const { data, isPending, isError, error } = useTools();

  function handleSelectTool(tool: ToolResource) {
    setSelectedTool((current) => (current === tool.name ? null : tool.name));
  }

  const tools = data ?? [];
  const selected = tools.find((t) => t.name === selectedTool) ?? null;

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-lg font-semibold">Tools</h1>
        <p className="text-sm text-muted-foreground">
          The contract registry the agent is bound to — every tool it can call, its policy
          metadata and its input/output schema. Read-only: nothing here executes a tool.
        </p>
      </div>

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
            <CardTitle>Could not load the tool catalog</CardTitle>
          </CardHeader>
          <CardContent className="text-sm text-muted-foreground">
            {error instanceof Error ? error.message : "Unknown error"}. Is the OpsPilot API
            running at the configured base URL?
          </CardContent>
        </Card>
      )}

      {!isPending && !isError && tools.length === 0 && (
        <Card>
          <CardContent className="pt-6 text-sm text-muted-foreground">
            No tools are registered.
          </CardContent>
        </Card>
      )}

      {!isPending && !isError && tools.length > 0 && (
        <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
          <div className="lg:col-span-2">
            <Card className="overflow-hidden p-0">
              <ToolCatalogTable
                tools={tools}
                selectedTool={selectedTool}
                onSelectTool={handleSelectTool}
              />
            </Card>
          </div>
          <div className="lg:col-span-1">
            {selected ? (
              <ToolInspector tool={selected} onClose={() => setSelectedTool(null)} />
            ) : (
              <Card className="sticky top-4">
                <CardContent className="pt-6 text-sm text-muted-foreground">
                  Select a tool to inspect its policy metadata and input/output schema.
                </CardContent>
              </Card>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
