"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  BookOpen,
  CalendarClock,
  Inbox,
  LayoutDashboard,
  Settings,
  Sparkles,
  Users,
} from "lucide-react";

import { cn } from "@/lib/utils";

/**
 * Primary navigation.
 *
 * Five destinations, in the order someone actually works them: what needs me, who is
 * talking, what needs chasing, what my AI knows, and the settings. Deliberately not more -
 * every extra item is a thing to read before finding the thing you want, and a sidebar that
 * lists the whole backend is how people end up not using it.
 *
 * `count` is the number waiting on you, not the total. A badge showing "847 leads" is
 * noise; a badge showing "3" next to Handoffs is the entire point of the nav.
 */
export interface NavItem {
  href: string;
  label: string;
  icon: typeof LayoutDashboard;
  count?: number;
  /** Announced to a screen reader alongside the number, since the dot alone is not enough. */
  countLabel?: string;
}

export const NAV_ITEMS: NavItem[] = [
  { href: "/dashboard", label: "Dashboard", icon: LayoutDashboard },
  { href: "/handoffs", label: "Handoffs", icon: Sparkles, countLabel: "waiting for a person" },
  { href: "/conversations", label: "Conversations", icon: Inbox },
  { href: "/leads", label: "Leads", icon: Users },
  { href: "/follow-ups", label: "Follow-ups", icon: CalendarClock },
  { href: "/knowledge", label: "Knowledge", icon: BookOpen },
];

export function SidebarNav({
  items = NAV_ITEMS,
  onNavigate,
  className,
}: {
  items?: NavItem[];
  /** Called after a link is chosen, so the mobile drawer can close itself. */
  onNavigate?: () => void;
  className?: string;
}) {
  const pathname = usePathname();

  return (
    <nav aria-label="Main" className={cn("flex flex-col gap-1", className)}>
      {items.map((item) => {
        const active = pathname === item.href || pathname.startsWith(`${item.href}/`);
        const Icon = item.icon;
        return (
          <Link
            key={item.href}
            href={item.href}
            onClick={onNavigate}
            aria-current={active ? "page" : undefined}
            className={cn(
              "group flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition-colors",
              "focus-visible:ring-ring focus-visible:ring-2 focus-visible:outline-none",
              active
                ? "bg-accent font-medium text-accent-foreground"
                : "text-muted-foreground hover:bg-accent/60 hover:text-foreground",
            )}
          >
            <Icon aria-hidden className="size-4 shrink-0" />
            <span className="flex-1 truncate">{item.label}</span>
            {item.count !== undefined && item.count > 0 && (
              <span
                className={cn(
                  "inline-flex h-5 min-w-5 items-center justify-center rounded-full px-1.5",
                  "text-[11px] font-semibold tabular-nums",
                  "bg-amber-100 text-amber-900 dark:bg-amber-950 dark:text-amber-200",
                )}
              >
                {item.count}
                <span className="sr-only"> {item.countLabel}</span>
              </span>
            )}
          </Link>
        );
      })}
    </nav>
  );
}

export function SidebarFooterLink({ onNavigate }: { onNavigate?: () => void }) {
  const pathname = usePathname();
  const active = pathname.startsWith("/settings");
  return (
    <Link
      href="/settings"
      onClick={onNavigate}
      aria-current={active ? "page" : undefined}
      className={cn(
        "flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition-colors",
        "focus-visible:ring-ring focus-visible:ring-2 focus-visible:outline-none",
        active
          ? "bg-accent font-medium text-accent-foreground"
          : "text-muted-foreground hover:bg-accent/60 hover:text-foreground",
      )}
    >
      <Settings aria-hidden className="size-4 shrink-0" />
      Settings
    </Link>
  );
}

export function WorkspaceMark({ name }: { name: string }) {
  // A stable colour per workspace so an operator working across two tenants can tell at a
  // glance which numbers belong to which. Derived from the name, so it is stable across
  // reloads and needs no stored preference.
  const hue = Array.from(name).reduce((acc, ch) => acc + ch.charCodeAt(0), 0) % 360;
  return (
    <span
      aria-hidden
      style={{ backgroundColor: `oklch(0.55 0.14 ${hue})` }}
      className="flex size-6 shrink-0 items-center justify-center rounded-md text-[10px] font-bold text-white"
    >
      {name.slice(0, 2).toUpperCase()}
    </span>
  );
}
