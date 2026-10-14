"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import {
  ArrowRight,
  BookOpen,
  CheckCircle2,
  Clock,
  Plug,
  Sparkles,
  TriangleAlert,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { LoadingRegion, PageSkeleton, Skeleton, StatStripSkeleton } from "@/components/ui/skeleton";
import { StatusBadge, type Tone } from "@/components/ui/status-badge";
import {
  attentionAction,
  attentionHref,
  dashboardOptions,
  type AttentionItem,
  type Dashboard,
} from "@/lib/api/dashboard";
import { channelLabels, leadStage, relativeTime, waitingFor } from "@/lib/format";

/**
 * The dashboard: what is happening, what needs me, and what changed.
 *
 * The ordering is the whole design. **Attention first**, then the numbers, then the threads.
 * A dashboard that leads with "12 new leads · 5 qualified" and buries "3 people are waiting"
 * is answering the question nobody asked. The first thing on this screen is the queue.
 *
 * Two rules the rest of the product follows, visible here:
 *
 * * **No fabricated metrics.** Every number is from the API. Where the product has no
 *   meaningful action attached to a number, the number is not shown - `qualified_leads` is on
 *   the strip because there is a leads list to filter with it, and nothing is on screen that
 *   a user cannot act on.
 * * **No scheduler internals.** "Follow-up" and "handoff" are words. `task_type`,
 *   `sequence_step` and `locked_by` never reach this component, which is why the backend
 *   translates them in its service rather than shipping raw rows for the UI to interpret.
 */
export function DashboardView() {
  const query = useQuery(dashboardOptions());

  if (query.isPending) return <DashboardSkeleton />;
  if (query.isError) {
    return (
      <div className="mx-auto w-full max-w-5xl px-4 py-8 lg:px-6">
        <ErrorState
          title="Could not load your dashboard"
          body="We could not reach the API. Nothing has been lost - your leads and handoffs are safe."
          onRetry={() => void query.refetch()}
        />
      </div>
    );
  }

  const data = query.data;
  const greeting = greetingFor(new Date().getHours());

  return (
    <div className="mx-auto w-full max-w-5xl px-4 py-6 lg:px-6 lg:py-8">
      <header className="flex flex-col gap-1">
        <h1 className="text-2xl font-semibold tracking-tight">{greeting}</h1>
        <p className="text-sm text-muted-foreground">{headline(data)}</p>
      </header>

      <section aria-label="Summary" className="mt-6">
        <StatStrip data={data} />
      </section>

      <section aria-labelledby="attention-heading" className="mt-8">
        <div className="flex items-center justify-between gap-3">
          <h2 id="attention-heading" className="text-sm font-semibold tracking-wide uppercase">
            Needs your attention
          </h2>
          {data.attention.length > 0 && (
            <span className="text-xs text-muted-foreground">
              {data.attention.length} item{data.attention.length === 1 ? "" : "s"}
            </span>
          )}
        </div>
        <div className="mt-3">
          <AttentionQueue items={data.attention} />
        </div>
      </section>

      <section aria-labelledby="activity-heading" className="mt-8">
        <h2 id="activity-heading" className="text-sm font-semibold tracking-wide uppercase">
          Lead activity
        </h2>
        <div className="mt-3">
          <LeadActivity data={data} />
        </div>
      </section>

      <section aria-labelledby="threads-heading" className="mt-8">
        <div className="flex items-center justify-between gap-3">
          <h2 id="threads-heading" className="text-sm font-semibold tracking-wide uppercase">
            Recent conversations
          </h2>
          <Link href="/conversations" className="text-xs text-muted-foreground hover:text-foreground">
            Open inbox
          </Link>
        </div>
        <div className="mt-3">
          <RecentThreads data={data} />
        </div>
      </section>

      {data.integration && (
        <section aria-labelledby="health-heading" className="mt-8">
          <h2 id="health-heading" className="text-sm font-semibold tracking-wide uppercase">
            Automation health
          </h2>
          <div className="mt-3">
            <HealthStrip data={data} />
          </div>
        </section>
      )}
    </div>
  );
}

// -- the attention queue -----------------------------------------------------

const SEVERITY: Record<AttentionItem["severity"], { tone: Tone; icon: typeof Sparkles; label: string }> = {
  critical: { tone: "danger", icon: Sparkles, label: "Human handoff" },
  warning: { tone: "warning", icon: Clock, label: "Follow-up" },
  info: { tone: "info", icon: CheckCircle2, label: "Worth a look" },
};

function AttentionQueue({ items }: { items: AttentionItem[] }) {
  if (items.length === 0) {
    return (
      <EmptyState
        tone="success"
        icon={CheckCircle2}
        title="Nothing needs you right now"
        body="When the AI decides a conversation needs a person, or a follow-up falls behind, it will appear here."
        action={{ label: "View conversations", href: "/conversations" }}
      />
    );
  }

  return (
    <ul className="flex flex-col divide-y overflow-hidden rounded-lg border">
      {items.map((item, index) => {
        const meta = SEVERITY[item.severity];
        const Icon = meta.icon;
        const href = attentionHref(item);
        return (
          <li
            key={`${item.kind}-${item.handoff_id ?? item.conversation_id ?? index}`}
            className="flex flex-wrap items-center gap-x-4 gap-y-2 p-3 transition-colors hover:bg-accent/40 sm:flex-nowrap"
          >
            <span
              aria-hidden
              className={
                item.severity === "critical"
                  ? "flex size-8 shrink-0 items-center justify-center rounded-full bg-red-50 text-red-600 dark:bg-red-950 dark:text-red-400"
                  : item.severity === "warning"
                    ? "flex size-8 shrink-0 items-center justify-center rounded-full bg-amber-50 text-amber-700 dark:bg-amber-950 dark:text-amber-400"
                    : "flex size-8 shrink-0 items-center justify-center rounded-full bg-muted text-muted-foreground"
              }
            >
              <Icon className="size-4" />
            </span>

            <span className="min-w-0 flex-1">
              <span className="flex flex-wrap items-center gap-2">
                <span className="truncate text-sm font-medium">{item.title}</span>
                {item.label && <StatusBadge tone={meta.tone}>{item.label}</StatusBadge>}
              </span>
              <span className="mt-0.5 block truncate text-xs text-muted-foreground">
                {item.detail}
              </span>
            </span>

            <span className="shrink-0 text-xs whitespace-nowrap text-muted-foreground">
              {item.waiting_seconds > 0 ? `waiting ${waitingFor(item.waiting_seconds)}` : null}
            </span>

            {href && (
              <Button asChild size="sm" variant={item.severity === "critical" ? "default" : "outline"}>
                <a href={href}>
                  {attentionAction(item)}
                  <ArrowRight aria-hidden className="size-3.5" />
                </a>
              </Button>
            )}
          </li>
        );
      })}
    </ul>
  );
}

// -- the strip ---------------------------------------------------------------

interface StatDefinition {
  label: string;
  value: number;
  hint: string;
  href: string;
  tone: Tone;
  /** A stat with nothing to do about it is not a stat. */
  actionable: boolean;
}

function StatStrip({ data }: { data: Dashboard }) {
  const { summary, lead_activity: activity } = data;
  const stats: StatDefinition[] = [
    {
      label: "New leads",
      value: summary.new_leads,
      hint: "not yet answered",
      href: "/leads?status=NEW",
      tone: "info",
      actionable: true,
    },
    {
      label: "Qualified",
      value: summary.qualified_leads,
      hint: "scored as serious",
      href: "/leads?status=QUALIFIED",
      tone: "success",
      actionable: true,
    },
    {
      label: "Follow-ups",
      value: summary.follow_ups_due,
      hint: summary.follow_ups_overdue > 0 ? `${summary.follow_ups_overdue} overdue` : "on schedule",
      href: "/follow-ups",
      tone: summary.follow_ups_overdue > 0 ? "warning" : "neutral",
      actionable: true,
    },
    {
      label: "Handoffs",
      value: summary.handoffs_open,
      hint:
        summary.handoffs_unassigned > 0
          ? `${summary.handoffs_unassigned} unassigned`
          : summary.handoffs_mine > 0
            ? `${summary.handoffs_mine} yours`
            : "all claimed",
      href: "/handoffs",
      tone: summary.handoffs_unassigned > 0 ? "danger" : "neutral",
      actionable: true,
    },
  ];

  return (
    <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
      {stats.map((stat) => (
        <Link
          key={stat.label}
          href={stat.href}
          className="group flex flex-col gap-1 rounded-lg border p-3 transition-colors hover:border-foreground/20 hover:bg-accent/40 focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
        >
          <span className="text-[11px] font-medium tracking-wide text-muted-foreground uppercase">
            {stat.label}
          </span>
          <span className="flex items-baseline gap-2">
            <span className="text-2xl font-semibold tabular-nums">{stat.value}</span>
            {stat.value > 0 && stat.tone !== "neutral" && (
              <StatusBadge tone={stat.tone} dot>
                {stat.hint}
              </StatusBadge>
            )}
            {stat.value === 0 && <span className="text-xs text-muted-foreground">{stat.hint}</span>}
          </span>
        </Link>
      ))}
      <span className="sr-only">
        {activity.new} new, {activity.qualified} qualified, {activity.replying} replying,{" "}
        {activity.handoff} needing a person.
      </span>
    </div>
  );
}

// -- lead activity -----------------------------------------------------------

const STAGES = [
  { key: "new", label: "New", tone: "info" as Tone, status: "NEW" },
  { key: "qualified", label: "Qualified", tone: "success" as Tone, status: "QUALIFIED" },
  { key: "replying", label: "Nurturing", tone: "neutral" as Tone, status: "REPLIED" },
  { key: "handoff", label: "Needs a person", tone: "warning" as Tone, status: "HUMAN_HANDOFF" },
] satisfies { key: keyof Dashboard["lead_activity"]; label: string; tone: Tone; status: string }[];

function LeadActivity({ data }: { data: Dashboard }) {
  const activity = data.lead_activity;
  const total = STAGES.reduce((sum, stage) => sum + activity[stage.key], 0);

  if (total === 0) {
    return (
      <EmptyState
        icon={Sparkles}
        title="No leads yet"
        body="When someone fills in one of your Meta lead forms, they will land here and the AI will start qualifying them."
        secondaryAction={{ label: "Connect Meta", href: "/settings?tab=meta" }}
      />
    );
  }

  return (
    <div className="flex flex-col gap-3 rounded-lg border p-4">
      <div className="flex h-2 w-full overflow-hidden rounded-full bg-muted">
        {STAGES.map((stage) => {
          const value = activity[stage.key];
          if (value === 0) return null;
          return (
            <span
              key={stage.key}
              className={
                stage.tone === "success"
                  ? "bg-emerald-500"
                  : stage.tone === "warning"
                    ? "bg-amber-500"
                    : stage.tone === "info"
                      ? "bg-sky-500"
                      : "bg-muted-foreground/40"
              }
              style={{ width: `${(value / total) * 100}%` }}
              title={`${stage.label}: ${value}`}
            />
          );
        })}
      </div>
      <div className="flex flex-wrap gap-x-5 gap-y-2">
        {STAGES.map((stage) => (
          <Link
            key={stage.key}
            href={`/leads?status=${stage.status}`}
            className="flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground"
          >
            <StatusBadge tone={stage.tone} dot>
              {stage.label}
            </StatusBadge>
            <span className="font-medium tabular-nums">{activity[stage.key]}</span>
          </Link>
        ))}
      </div>
    </div>
  );
}

// -- recent threads ----------------------------------------------------------

function RecentThreads({ data }: { data: Dashboard }) {
  const threads = data.recent_conversations;
  if (threads.length === 0) {
    return (
      <EmptyState
        icon={BookOpen}
        title="No conversations yet"
        body="Threads appear as soon as someone messages your connected Page or Instagram account."
        action={{ label: "Connect Meta", href: "/settings?tab=meta" }}
      />
    );
  }
  return (
    <ul className="divide-y overflow-hidden rounded-lg border">
      {threads.map((thread) => {
        const stage = leadStage(thread.lead_status);
        // The stage badge already says "Needs a person" for a lead in HUMAN_HANDOFF, so the
        // handoff flag would render the same words twice on one row. The flag is only
        // informative for a lead whose *status* has not caught up yet - which happens
        // because the handoff record and the lead status are written in the same
        // transaction but read through two different tables.
        const showHandoff = thread.has_active_handoff && stage.label !== "Needs a person";
        return (
          <li key={thread.id}>
            <Link
              href={`/conversations/${thread.id}`}
              className="flex items-center gap-3 p-3 transition-colors hover:bg-accent/40"
            >
              <span className="flex min-w-0 flex-1 flex-col">
                <span className="flex flex-wrap items-center gap-2">
                  <span className="truncate text-sm font-medium">
                    {thread.lead_name ?? "Messenger conversation"}
                  </span>
                  <StatusBadge tone={stage.tone}>{stage.label}</StatusBadge>
                  {showHandoff && <StatusBadge tone="warning">Needs a person</StatusBadge>}
                </span>
                <span className="mt-0.5 truncate text-xs text-muted-foreground">
                  {thread.last_message_text ??
                    (channelLabels[thread.channel] ?? thread.channel) + " · no messages yet"}
                </span>
              </span>
              <span className="shrink-0 text-xs whitespace-nowrap text-muted-foreground">
                {relativeTime(thread.last_message_at)}
              </span>
            </Link>
          </li>
        );
      })}
    </ul>
  );
}

// -- health ------------------------------------------------------------------

function HealthStrip({ data }: { data: Dashboard }) {
  const integration = data.integration;
  if (!integration) return null;
  const rows = [
    {
      label: "Meta connection",
      ok: integration.meta_connected && !integration.meta_needs_attention,
      detail: integration.meta_connected
        ? integration.last_webhook_at
          ? `last message ${relativeTime(integration.last_webhook_at)}`
          : "connected, waiting for the first message"
        : "needs reconnecting - no new leads are arriving",
      href: "/settings?tab=meta",
    },
    {
      label: "Knowledge",
      ok: integration.knowledge_documents > 0,
      detail:
        integration.knowledge_documents > 0
          ? `${integration.knowledge_documents} document${integration.knowledge_documents === 1 ? "" : "s"} the AI can use`
          : "the AI has nothing to answer from",
      href: "/knowledge",
    },
    {
      label: "Follow-ups",
      ok: data.summary.follow_ups_overdue === 0,
      detail:
        data.summary.follow_ups_overdue === 0
          ? "nothing overdue"
          : `${data.summary.follow_ups_overdue} behind schedule`,
      href: "/follow-ups",
    },
  ];

  return (
    <ul className="divide-y overflow-hidden rounded-lg border">
      {rows.map((row) => (
        <li key={row.label} className="flex items-center gap-3 p-3">
          <span
            aria-hidden
            className={
              row.ok
                ? "flex size-7 shrink-0 items-center justify-center rounded-full bg-emerald-50 text-emerald-600 dark:bg-emerald-950 dark:text-emerald-400"
                : "flex size-7 shrink-0 items-center justify-center rounded-full bg-amber-50 text-amber-700 dark:bg-amber-950 dark:text-amber-400"
            }
          >
            {row.ok ? (
              <CheckCircle2 className="size-4" />
            ) : row.href === "/settings?tab=meta" ? (
              <Plug className="size-4" />
            ) : (
              <TriangleAlert className="size-4" />
            )}
          </span>
          <span className="flex min-w-0 flex-1 flex-col">
            <span className="text-sm font-medium">{row.label}</span>
            <span className="truncate text-xs text-muted-foreground">{row.detail}</span>
          </span>
          <Button asChild size="sm" variant="ghost">
            <a href={row.href}>Manage</a>
          </Button>
        </li>
      ))}
    </ul>
  );
}

// -- copy and loading --------------------------------------------------------

function greetingFor(hour: number): string {
  if (hour < 12) return "Good morning";
  if (hour < 18) return "Good afternoon";
  return "Good evening";
}

/** One sentence of orientation, built from what is actually true right now. */
function headline(data: Dashboard): string {
  const { summary, attention } = data;
  const critical = attention.filter((item) => item.severity === "critical").length;
  // `new_leads_today` is arrivals; `new_leads` is leads still sitting unanswered. The words
  // "today" and "waiting" belong to the first and second respectively, and using either count
  // with the other's words is how a dashboard ends up confidently wrong.
  const arrived = summary.new_leads_today;
  const arrivedText = `${arrived} new lead${arrived === 1 ? "" : "s"} today`;
  if (critical > 0) {
    return `${critical} conversation${critical === 1 ? "" : "s"} ${critical === 1 ? "is" : "are"} waiting on a person. ${arrivedText}.`;
  }
  if (summary.follow_ups_due > 0) {
    return `Your pipeline is running. ${arrivedText}, ${summary.follow_ups_due} follow-up${summary.follow_ups_due === 1 ? "" : "s"} scheduled today.`;
  }
  return `Your pipeline is active. ${arrivedText}, nothing overdue.`;
}

function DashboardSkeleton() {
  return (
    <div className="mx-auto w-full max-w-5xl px-4 py-6 lg:px-6 lg:py-8">
      <LoadingRegion label="Loading your dashboard">
        <Skeleton className="h-7 w-44" />
        <Skeleton className="mt-2 h-4 w-72" />
        <div className="mt-6">
          <StatStripSkeleton />
        </div>
        <div className="mt-8">
          <Skeleton className="h-4 w-40" />
          <div className="mt-3">
            <PageSkeleton rows={3} />
          </div>
        </div>
        <div className="mt-8">
          <Skeleton className="h-4 w-32" />
          <div className="mt-3">
            <PageSkeleton rows={3} />
          </div>
        </div>
      </LoadingRegion>
    </div>
  );
}