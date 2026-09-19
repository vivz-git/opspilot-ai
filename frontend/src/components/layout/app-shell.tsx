import * as React from "react";

import { HealthIndicator } from "@/components/layout/health-indicator";
import { Nav } from "@/components/layout/nav";
import { Separator } from "@/components/ui/separator";

export function AppShell({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex min-h-screen bg-background">
      <aside className="hidden w-60 shrink-0 border-r border-border md:flex md:flex-col">
        <div className="flex h-14 items-center gap-2 px-4">
          <span className="font-mono text-sm font-semibold tracking-tight">OpsPilot</span>
          <span className="text-xs text-muted-foreground">console</span>
        </div>
        <Separator />
        <Nav />
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-14 items-center justify-between border-b border-border px-4 md:px-6">
          <span className="font-mono text-sm font-semibold tracking-tight md:hidden">
            OpsPilot
          </span>
          <span className="hidden text-sm text-muted-foreground md:inline">
            Agent execution runtime — operator console
          </span>
          <HealthIndicator />
        </header>
        <main className="flex-1 px-4 py-6 md:px-6">{children}</main>
      </div>
    </div>
  );
}
