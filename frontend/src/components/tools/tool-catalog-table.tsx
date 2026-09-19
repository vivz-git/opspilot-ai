"use client";

import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import type { ToolResource } from "@/lib/api/client";

function RiskBadge({ risk }: { risk: string }) {
  const tone =
    risk === "high" ? "destructive" : risk === "medium" ? "warning" : ("outline" as const);
  return (
    <Badge variant={tone} className="font-mono uppercase">
      {risk}
    </Badge>
  );
}

export function ToolCatalogTable({
  tools,
  selectedTool,
  onSelectTool,
}: {
  tools: ToolResource[];
  selectedTool: string | null;
  onSelectTool: (tool: ToolResource) => void;
}) {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Tool</TableHead>
          <TableHead>Purpose</TableHead>
          <TableHead>Side effect</TableHead>
          <TableHead>Risk</TableHead>
          <TableHead>Approval</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {tools.map((tool) => (
          <TableRow
            key={tool.name}
            role="link"
            tabIndex={0}
            data-state={selectedTool === tool.name ? "selected" : undefined}
            aria-current={selectedTool === tool.name ? "true" : undefined}
            aria-label={`Inspect tool ${tool.name}`}
            className="cursor-pointer"
            onClick={() => onSelectTool(tool)}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                onSelectTool(tool);
              }
            }}
          >
            <TableCell className="font-mono text-sm font-medium">{tool.name}</TableCell>
            <TableCell className="max-w-sm truncate text-sm text-muted-foreground">
              {tool.purpose}
            </TableCell>
            <TableCell className="font-mono text-xs text-muted-foreground">
              {tool.side_effect}
            </TableCell>
            <TableCell>
              <RiskBadge risk={tool.risk} />
            </TableCell>
            <TableCell>
              {tool.requires_approval ? (
                <Badge variant="warning" className="font-mono text-[10px]">
                  gated
                </Badge>
              ) : (
                <span className="font-mono text-xs text-muted-foreground">—</span>
              )}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}
