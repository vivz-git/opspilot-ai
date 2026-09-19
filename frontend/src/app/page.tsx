import Link from "next/link";
import { Activity, CheckSquare, FlaskConical, Wrench } from "lucide-react";

import { Card, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

const SURFACES = [
  {
    href: "/runs",
    icon: Activity,
    title: "Runs",
    description: "Every submitted request as a planned, budgeted, terminating unit of work.",
  },
  {
    href: "/approvals",
    icon: CheckSquare,
    title: "Approvals",
    description: "Mutating or outbound steps paused for a human decision, with the exact payload.",
  },
  {
    href: "/evaluations",
    icon: FlaskConical,
    title: "Evaluations",
    description: "Suite runs and metrics that measure agent behaviour against expectations.",
  },
  {
    href: "/tools",
    icon: Wrench,
    title: "Tools",
    description: "The contract registry the agent is bound to — schemas, risk and approval flags.",
  },
] as const;

export default function OverviewPage() {
  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-lg font-semibold">Overview</h1>
        <p className="text-sm text-muted-foreground">
          OpsPilot executes operator requests as inspectable, approvable runs. This console
          renders what the backend decides — it never derives agent state on its own.
        </p>
      </div>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {SURFACES.map(({ href, icon: Icon, title, description }) => (
          <Link key={href} href={href}>
            <Card className="h-full transition-colors hover:bg-accent/50">
              <CardHeader>
                <Icon className="h-5 w-5 text-muted-foreground" aria-hidden />
                <CardTitle>{title}</CardTitle>
                <CardDescription>{description}</CardDescription>
              </CardHeader>
            </Card>
          </Link>
        ))}
      </div>
    </div>
  );
}
