"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "next/navigation";
import { CheckCircle2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { EmptyState, ErrorState, InlineError } from "@/components/ui/states";
import { LoadingRegion, Skeleton } from "@/components/ui/skeleton";
import { StatusBadge } from "@/components/ui/status-badge";
import {
  handoffsOptions,
  useClaimHandoff,
  useResolveHandoff,
  useStartHandoff,
  type Handoff,
  type HandoffStatus,
} from "@/lib/api/handoffs";
import { useCurrentUser } from "@/lib/auth/hooks";
import { handoffReason, relativeTime, waitingFor } from "@/lib/format";

/**
 * The handoff queue.
 *
 * A work queue, not a table. Each row carries the three things an agent needs to decide
 * whether to take it - who, why, and what they actually said - so claiming is a one-click
 * decision rather than a click followed by three navigations.
 *
 * `focus` in the URL is how the dashboard deep-links. The attention item says "Rahul is
 * waiting" and links here with the id; the queue opens that one and highlights it. Without
 * it the operator lands on a queue of twenty and has to find the one they clicked.
 */
const FILTERS = [
  { value: "active", label: "Needs attention" },
  { value: "all", label: "All" },
] as const;

type FilterValue = (typeof FILTERS)[number]["value"];

export function HandoffsView() {
  const params = useSearchParams();
  const focus = params.get("focus");
  const [filter, setFilter] = useState<FilterValue>("active");
  const [picked, setPicked] = useState<string | null>(null);

  const query = useQuery({ ...handoffsOptions(), staleTime: 15_000 });
  const claim = useClaimHandoff();
  const start = useStartHandoff();
  const resolve = useResolveHandoff();

  // The queue is "what nobody has done yet" by default. A resolved handoff is history, and
  // it is one click away rather than occupying the list someone works from.
  const all = query.data?.items ?? [];
  const visible = all.filter((handoff) =>
    filter === "all" ? true : handoff.status !== "RESOLVED" && handoff.status !== "CANCELLED",
  );
  // A deep link from the dashboard wins over a local pick: the operator clicked a specific
  // escalation and should land on that one, not on the top of an unrelated queue.
  const selected =
    (focus ? all.find((h) => h.id === focus) : null) ??
    (picked ? all.find((h) => h.id === picked) : null) ??
    visible[0];

  if (query.isPending) return <QueueSkeleton />;
  if (query.isError) {
    return (
      <div className="mx-auto w-full max-w-4xl px-4 py-8 lg:px-6">
        <ErrorState
          title="Could not load the handoff queue"
          body="The API did not respond. Any handoffs are still waiting - nothing has been lost."
          onRetry={() => void query.refetch()}
        />
      </div>
    );
  }

  const unassigned = visible.filter((h) => h.assigned_user_id === null).length;
  const busy = claim.isPending || start.isPending || resolve.isPending;
  const error = claim.error ?? start.error ?? resolve.error;

  return (
    <div className="mx-auto w-full max-w-4xl px-4 py-6 lg:px-6 lg:py-8">
      <header className="flex flex-col gap-1">
        <h1 className="text-2xl font-semibold tracking-tight">Human handoffs</h1>
        <p className="text-sm text-muted-foreground">
          {visible.length === 0
            ? "Conversations the AI decided need a person."
            : `${visible.length} waiting · ${unassigned} unassigned`}
        </p>
      </header>

      <div className="mt-4 flex gap-1.5" role="group" aria-label="Filter handoffs">
        {FILTERS.map((option) => (
          <button
            key={option.value}
            type="button"
            aria-pressed={filter === option.value}
            onClick={() => setFilter(option.value)}
            className={
              filter === option.value
                ? "rounded-full border px-3 py-1 text-xs font-medium"
                : "rounded-full border border-muted px-3 py-1 text-xs text-muted-foreground hover:bg-accent"
            }
          >
            {option.label}
          </button>
        ))}
      </div>

      {visible.length === 0 ? (
        <div className="mt-5">
          <EmptyState
            tone="success"
            icon={CheckCircle2}
            title="No handoffs waiting"
            body="When the AI judges a conversation needs a person - because they asked, or because they scored high, or because something broke - it lands here with the context to act on."
            action={{ label: "View conversations", href: "/conversations" }}
          />
        </div>
      ) : (
        <div className="mt-5 grid gap-4 md:grid-cols-[minmax(0,20rem)_minmax(0,1fr)]">
          <ul className="divide-y overflow-hidden rounded-lg border md:max-h-[32rem] md:overflow-y-auto">
            {visible.map((handoff) => {
              const reason = handoffReason(handoff.reason);
              const isFocused = selected?.id === handoff.id;
              return (
                <li key={handoff.id}>
                  <button
                    type="button"
                    onClick={() => setPicked(handoff.id)}
                    aria-current={isFocused ? "true" : undefined}
                    className={
                      isFocused
                        ? "flex w-full flex-col gap-1 bg-accent/60 p-3 text-left"
                        : "flex w-full flex-col gap-1 p-3 text-left transition-colors hover:bg-accent/40"
                    }
                  >
                    <span className="flex items-center gap-2">
                      <span className="truncate text-sm font-medium">
                        {handoff.lead_name ?? "Conversation"}
                      </span>
                      <StatusBadge tone={reason.tone}>{reason.label}</StatusBadge>
                    </span>
                    <span className="text-xs text-muted-foreground">
                      waiting {waitingFor(secondsSince(handoff.created_at))} ·{" "}
                      {handoff.assigned_user_id ? "claimed" : "unclaimed"}
                    </span>
                    {handoff.latest_inbound_text && (
                      <span className="line-clamp-2 text-xs text-muted-foreground italic">
                        “{handoff.latest_inbound_text}”
                      </span>
                    )}
                    {handoff.acknowledgement_failure && (
                      <span className="text-xs text-destructive">
                        The lead was not told someone is coming.
                      </span>
                    )}
                  </button>
                </li>
              );
            })}
          </ul>

          {selected && (
            <HandoffDetail
              handoff={selected}
              busy={busy}
              error={error}
              onClaim={() => claim.mutate({ handoffId: selected.id })}
              onStart={() => start.mutate({ handoffId: selected.id })}
              onResolve={() => resolve.mutate({ handoffId: selected.id, note: undefined })}
              onOpenConversation={
                selected.conversation_id
                  ? `/conversations/${selected.conversation_id}`
                  : undefined
              }
            />
          )}
        </div>
      )}
    </div>
  );
}

function HandoffDetail({
  handoff,
  busy,
  error,
  onClaim,
  onStart,
  onResolve,
  onOpenConversation,
}: {
  handoff: Handoff;
  busy: boolean;
  error: unknown;
  onClaim: () => void;
  onStart: () => void;
  onResolve: () => void;
  onOpenConversation?: string;
}) {
  const { user } = useCurrentUser();
  const reason = handoffReason(handoff.reason);
  const terminal = handoff.status === "RESOLVED" || handoff.status === "CANCELLED";
  const mine = Boolean(user && handoff.assigned_user_id === user.id);

  // The acknowledgement failure is stated plainly and early. It changes how the agent opens
  // the conversation - a lead who was never told someone is coming is a phone call, not a
  // message - and it is not something they could infer from the queue.
  const ackFailed = Boolean(handoff.acknowledgement_failure);

  return (
    <div className="flex flex-col gap-5 rounded-lg border p-4">
      <header>
        <h2 className="text-lg font-semibold">{handoff.lead_name ?? "Conversation"}</h2>
        <p className="text-sm text-muted-foreground">
          {reason.blurb} Raised {relativeTime(handoff.created_at)}.
        </p>
      </header>

      {ackFailed && (
        <p className="rounded-md border border-red-200 bg-red-50/60 p-3 text-sm text-red-900 dark:border-red-900 dark:bg-red-950/40 dark:text-red-200">
          The acknowledgement to the lead was not delivered. They may not know anyone is
          coming - consider calling rather than messaging.
        </p>
      )}

      {handoff.qualification_total !== null && (
        <div>
          <p className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
            Lead score
          </p>
          <p className="mt-1 text-sm">
            {handoff.qualification_total}/100 at the point of escalation
          </p>
        </div>
      )}

      {handoff.latest_inbound_text && (
        <div>
          <p className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
            What they said
          </p>
          <blockquote className="mt-1 border-l-2 pl-3 text-sm">
            {handoff.latest_inbound_text}
          </blockquote>
        </div>
      )}

      <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
        <Field label="Status">{handoffStatusLabel(handoff.status)}</Field>
        <Field label="Owner">
          {handoff.assigned_user_id ? (mine ? "You" : "A teammate") : "Nobody yet"}
        </Field>
        <Field label="Lead told">{handoff.acknowledged_at ? "Yes" : "No"}</Field>
        <Field label="Raised">{relativeTime(handoff.created_at)}</Field>
      </dl>

      {handoff.resolution_note && (
        <p className="text-sm text-muted-foreground">Note: {handoff.resolution_note}</p>
      )}

      {!terminal && (
        <div className="flex flex-wrap items-center gap-2 border-t pt-4">
          {handoff.assigned_user_id === null && (
            <Button size="sm" disabled={busy} onClick={onClaim}>
              {busy ? "Claiming…" : "Claim"}
            </Button>
          )}
          {handoff.status === "ASSIGNED" && (
            <Button size="sm" variant="outline" disabled={busy} onClick={onStart}>
              Start
            </Button>
          )}
          {onOpenConversation && (
            <Button asChild size="sm" variant="outline">
              <a href={onOpenConversation}>Open conversation</a>
            </Button>
          )}
          <Button size="sm" variant="ghost" disabled={busy} onClick={onResolve}>
            Resolve
          </Button>
          {error != null && <InlineError error={error} />}
        </div>
      )}
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

function handoffStatusLabel(status: HandoffStatus): string {
  return (
    { OPEN: "Open", ASSIGNED: "Claimed", IN_PROGRESS: "In progress", RESOLVED: "Resolved", CANCELLED: "Cancelled" }[
      status
    ] ?? status
  );
}

function secondsSince(iso: string): number {
  return Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
}

function QueueSkeleton() {
  return (
    <div className="mx-auto w-full max-w-4xl px-4 py-6 lg:px-6 lg:py-8">
      <LoadingRegion label="Loading handoffs">
        <Skeleton className="h-8 w-56" />
        <div className="mt-5 grid gap-4 md:grid-cols-[minmax(0,20rem)_minmax(0,1fr)]">
          <Skeleton className="h-72 w-full rounded-lg" />
          <Skeleton className="h-72 w-full rounded-lg" />
        </div>
      </LoadingRegion>
    </div>
  );
}