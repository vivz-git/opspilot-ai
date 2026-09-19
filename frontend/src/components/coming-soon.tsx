import type { LucideIcon } from "lucide-react";

import { Card, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

export function ComingSoon({
  icon: Icon,
  title,
  description,
  note,
}: {
  icon: LucideIcon;
  title: string;
  description: string;
  note: string;
}) {
  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-lg font-semibold">{title}</h1>
        <p className="text-sm text-muted-foreground">{description}</p>
      </div>
      <Card>
        <CardHeader>
          <Icon className="h-5 w-5 text-muted-foreground" aria-hidden />
          <CardTitle>Not wired up yet</CardTitle>
          <CardDescription>{note}</CardDescription>
        </CardHeader>
      </Card>
    </div>
  );
}
