"use client";

import { useQuery } from "@tanstack/react-query";
import { CalendarClock } from "lucide-react";

import { Button } from "@/components/ui/button";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { LoadingRegion, Skeleton } from "@/components/ui/skeleton";
import { StatusBadge, type Tone } from "@/components/ui/status-badge";
import { scheduledTasksOptions, taskKindLabel, type ScheduledTask } from "@/lib/api/follow-ups";
import { leadsOptions } from "@/lib/api/leads";
import { absoluteTime, scheduleTime } from "@/lib/format";

/**
 * Follow-ups, in the language of a person checking their own automation.
 *
 * The backend's rows are `scheduled_tasks` with a `task_type` and a `sequence_step`. None of
 * that reaches this screen: it reads "Follow-up #2", "Overdue", "Tomorrow". The translation
 * lives in `lib/api/follow-ups.ts` so there is exactly one place that knows a `FOLLOW_UP`
 * task is called a follow-up, and a scheduler rename cannot leak into the product's words.
 *
 * Grouping is by *urgency*, not by status, because the question is "what do I do now":
 * overdue, due today, upcoming, and separately what already happened. A handoff-suppressed
 * task is surfaced in its own group with the reason, because a follow-up that will never send
 * is something an operator should be able to explain rather than wonder about.
 */
type Group = "overdue" | "today" | "upcoming" | "stopped" | "done";

const GROUP_ORDER: Group[] = ["overdue", "today", "upcoming", "stopped", "done"];

const GROUPS: Record<Group, { title: string; tone: Tone; blurb: string }> = {
  overdue: { title: "Overdue", tone: "danger", blurb: "Should have gone out already." },
  today: { title: "Due today", tone: "warning", blurb: "Scheduled to send later today." },
  upcoming: { title: "Upcoming", tone: "info", blurb: "The rest of the sequence." },
  stopped: { title: "Stopped", tone: "neutral", blurb: "Will not send. Here is why." },
  done: { title: "Sent", tone: "success", blurb: "Already delivered." },
};

export function FollowUpsView() {
  const tasks = useQuery(scheduledTasksOptions());
  // The task rows carry ids, not names. One cached lead list turns them into people, and the
  // list view has already fetched it, so this is a cache hit rather than an extra request.
  const leads = useQuery(leadsOptions({ limit: 200 }));
  const leadName = new Map((leads.data?.items ?? []).map((l) => [l.id, l.name]));

  if (tasks.isPending) return <FollowUpsSkeleton />;
  if (tasks.isError) {
    return (
      <div className="mx-auto w-full max-w-4xl px-4 py-8 lg:px-6">
        <ErrorState
          title="Could not load follow-ups"
          body="The API did not respond. Any scheduled follow-ups are still scheduled."
          onRetry={() => void tasks.refetch()}
        />
      </div>
    );
  }

  const groups = groupTasks(tasks.data.items, leadName);
  const total = tasks.data.items.length;

  return (
    <div className="mx-auto w-full max-w-4xl px-4 py-6 lg:px-6 lg:py-8">
      <header className="flex flex-col gap-1">
        <h1 className="text-2xl font-semibold tracking-tight">Follow-ups</h1>
        <p className="text-sm text-muted-foreground">
          The AI keeps a conversation warm after it replies. {total} scheduled
          {total === 1 ? "" : ""}.
        </p>
      </header>

      {total === 0 ? (
        <div className="mt-6">
          <EmptyState
            icon={CalendarClock}
            title="No follow-ups scheduled"
            body="A follow-up is created after the AI answers a lead for the first time. Reply to someone and the sequence starts here."
            action={{ label: "View conversations", href: "/conversations" }}
          />
        </div>
      ) : (
        <div className="mt-6 flex flex-col gap-6">
          {GROUP_ORDER.map((group) => {
            const items = groups[group];
            if (items.length === 0) return null;
            const meta = GROUPS[group];
            return (
              <section key={group} aria-labelledby={`group-${group}`}>
                <div className="flex items-center gap-2">
                  <h2 id={`group-${group}`} className="text-sm font-semibold">
                    {meta.title}
                  </h2>
                  <StatusBadge tone={meta.tone}>{items.length}</StatusBadge>
                  <span className="text-xs text-muted-foreground">{meta.blurb}</span>
                </div>
                <ul className="mt-2 divide-y overflow-hidden rounded-lg border">
                  {items.map((task) => (
                    <TaskRow key={task.id} task={task} name={leadName.get(task.lead_id ?? "")} />
                  ))}
                </ul>
              </section>
            );
          })}
        </div>
      )}
    </div>
  );
}

function TaskRow({ task, name }: { task: ScheduledTask; name?: string | null }) {
  const when = scheduleTime(task.scheduled_at);
  const stopped = task.status === "CANCELLED" || task.status === "FAILED";
  return (
    <li className="flex flex-wrap items-center gap-x-4 gap-y-1 p-3">
      <span className="flex min-w-0 flex-1 flex-col">
        <span className="truncate text-sm font-medium">{name ?? "Conversation"}</span>
        <span className="text-xs text-muted-foreground">
          {taskKindLabel(task)}
          {stopped && task.last_error ? ` · ${explainStop(task.last_error)}` : ""}
        </span>
      </span>
      <span
        className="shrink-0 text-xs whitespace-nowrap text-muted-foreground"
        title={absoluteTime(task.scheduled_at)}
      >
        {when}
      </span>
      {task.conversation_id && (
        <Button asChild size="sm" variant="ghost">
          <a href={`/conversations/${task.conversation_id}`}>Open</a>
        </Button>
      )}
    </li>
  );
}

/** Scheduler reasons, in the product's words. */
function explainStop(reason: string): string {
  const map: Record<string, string> = {
    human_handoff: "a person took over the conversation",
    lead_replied: "the lead replied, so the sequence stopped",
    lead_closed: "the lead is closed",
    lead_ghosted: "the lead went quiet",
    outside_tagged_window: "Meta would no longer accept a tagged message",
    lead_human_handoff: "a person took over the conversation",
    conversation_missing: "the conversation is gone",
  };
  return map[reason] ?? "it will not send";
}

function groupTasks(
  tasks: ScheduledTask[],
  leadName: Map<string, string | null>,
): Record<Group, ScheduledTask[]> {
  void leadName;
  const now = Date.now();
  const endOfDay = new Date();
  endOfDay.setHours(23, 59, 59, 999);

  const groups: Record<Group, ScheduledTask[]> = {
    overdue: [],
    today: [],
    upcoming: [],
    stopped: [],
    done: [],
  };

  for (const task of tasks) {
    if (task.status === "COMPLETED") {
      groups.done.push(task);
    } else if (task.status === "CANCELLED" || task.status === "FAILED") {
      groups.stopped.push(task);
    } else {
      const due = new Date(task.scheduled_at).getTime();
      if (due < now) groups.overdue.push(task);
      else if (due <= endOfDay.getTime()) groups.today.push(task);
      else groups.upcoming.push(task);
    }
  }
  return groups;
}

function FollowUpsSkeleton() {
  return (
    <div className="mx-auto w-full max-w-4xl px-4 py-6 lg:px-6 lg:py-8">
      <LoadingRegion label="Loading follow-ups">
        <Skeleton className="h-8 w-48" />
        <div className="mt-6 flex flex-col gap-4">
          {Array.from({ length: 4 }, (_, i) => (
            <Skeleton key={i} className="h-14 w-full rounded-lg" />
          ))}
        </div>
      </LoadingRegion>
    </div>
  );
}
