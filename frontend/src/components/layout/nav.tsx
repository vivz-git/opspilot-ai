"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  Activity,
  CheckSquare,
  FlaskConical,
  LayoutDashboard,
  Wrench,
} from "lucide-react";

import { cn } from "@/lib/utils";

const NAV_ITEMS = [
  { href: "/", label: "Overview", icon: LayoutDashboard },
  { href: "/runs", label: "Runs", icon: Activity },
  { href: "/approvals", label: "Approvals", icon: CheckSquare },
  { href: "/evaluations", label: "Evaluations", icon: FlaskConical },
  { href: "/tools", label: "Tools", icon: Wrench },
] as const;

export function Nav() {
  const pathname = usePathname();

  return (
    <nav className="flex flex-col gap-1 px-2 py-3">
      {NAV_ITEMS.map((item) => {
        const active = item.href === "/" ? pathname === "/" : pathname.startsWith(item.href);
        const Icon = item.icon;
        return (
          <Link
            key={item.href}
            href={item.href}
            aria-current={active ? "page" : undefined}
            className={cn(
              "flex items-center gap-2 rounded-md border-l-2 py-2 pl-2.5 pr-3 text-sm font-medium transition-colors",
              active
                ? "border-primary bg-accent text-foreground"
                : "border-transparent text-muted-foreground hover:bg-accent/60 hover:text-foreground"
            )}
          >
            <Icon
              className={cn("h-4 w-4 shrink-0", active ? "text-primary" : "text-muted-foreground")}
              aria-hidden
            />
            {item.label}
          </Link>
        );
      })}
    </nav>
  );
}
