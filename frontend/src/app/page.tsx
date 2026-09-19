import Link from "next/link";
import { Activity, ArrowUpRight, CheckSquare, FlaskConical, Wrench } from "lucide-react";

import { Card, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";

const SECONDARY_SURFACES = [
  {
    href: "/approvals",
    icon: CheckSquare,
    title: "Approvals",
    description: "Steps paused for a human decision, with the exact payload.",
  },
  {
    href: "/evaluations",
    icon: FlaskConical,
    title: "Evaluations",
    description: "Suite runs and metrics against expected agent behaviour.",
  },
  {
    href: "/tools",
    icon: Wrench,
    title: "Tools",
    description: "The contract registry the agent is bound to.",
  },
] as const;

export default function OverviewPage() {
  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-baseline justify-between gap-4">
        <div>
          <h1 className="text-lg font-semibold">Overview</h1>
          <p className="max-w-xl text-sm text-muted-foreground">
            OpsPilot executes operator requests as inspectable, approvable runs. This console
            renders what the backend decides — it never derives agent state on its own.
          </p>
        </div>
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <Link href="/runs" className="lg:col-span-2">
          <Card className="group h-full transition-colors hover:border-primary/40 hover:bg-accent/30">
            <CardHeader className="gap-3">
              <div className="flex items-center justify-between">
                <div className="flex h-9 w-9 items-center justify-center rounded-md bg-primary/10 text-primary">
                  <Activity className="h-5 w-5" aria-hidden />
                </div>
                <ArrowUpRight
                  className="h-4 w-4 text-muted-foreground transition-transform group-hover:translate-x-0.5 group-hover:-translate-y-0.5"
                  aria-hidden
                />
              </div>
              <CardTitle className="text-base">Runs</CardTitle>
              <CardDescription>
                Every submitted request as a planned, budgeted, terminating unit of work — the
                primary surface of this console.
              </CardDescription>
            </CardHeader>
          </Card>
        </Link>

        <div className="flex flex-col gap-4">
          {SECONDARY_SURFACES.map(({ href, icon: Icon, title, description }, i) => (
            <Link key={href} href={href} className={cn(i === 0 && "flex-1")}>
              <Card className="group h-full transition-colors hover:border-primary/40 hover:bg-accent/30">
                <CardHeader className="flex-row items-center gap-3 space-y-0 py-4">
                  <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-accent text-muted-foreground">
                    <Icon className="h-4 w-4" aria-hidden />
                  </div>
                  <div className="flex flex-col gap-0.5">
                    <CardTitle className="text-sm">{title}</CardTitle>
                    <CardDescription className="text-xs">{description}</CardDescription>
                  </div>
                </CardHeader>
              </Card>
            </Link>
          ))}
        </div>
      </div>
    </div>
  );
}
