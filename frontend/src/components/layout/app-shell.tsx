import * as React from "react";

import { HealthIndicator } from "@/components/layout/health-indicator";
import { Nav } from "@/components/layout/nav";

export function AppShell({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex min-h-screen bg-background">
      <aside className="hidden w-60 shrink-0 border-r border-border/60 bg-card/40 md:flex md:flex-col">
        <div className="flex h-14 items-center gap-2 border-b border-border/60 px-4">
          <span className="rounded-sm bg-primary px-1.5 py-0.5 font-mono text-xs font-semibold text-primary-foreground">
            OP
          </span>
          <span className="font-mono text-sm font-semibold tracking-tight">OpsPilot</span>
        </div>
        <Nav />
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-14 items-center justify-between border-b border-border/60 bg-card/20 px-4 md:px-6">
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
