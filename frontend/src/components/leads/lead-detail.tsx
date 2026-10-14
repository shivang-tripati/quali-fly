"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, MessageSquare } from "lucide-react";

import { Button } from "@/components/ui/button";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { LoadingRegion, Skeleton } from "@/components/ui/skeleton";
import { StatusBadge } from "@/components/ui/status-badge";
import { leadOptions } from "@/lib/api/leads";
import { handoffsOptions } from "@/lib/api/handoffs";
import { conversationsOptions } from "@/lib/api/meta";
import {
  DIMENSION_MAX,
  absoluteTime,
  displayName,
  handoffReason,
  leadStage,
  qualificationDimensions,
  relativeTime,
} from "@/lib/format";

/**
 * One lead, as a workspace rather than a form dump.
 *
 * The screen answers three questions in order: *who is this*, *what happened*, *what do I do
 * about it*. Qualification renders as bars because the raw JSONB is four integers the AI
 * merged - showing `{"budget": 21, ...}` would be handing the user the database and asking
 * them to read it.
 *
 * `qualification: null` is a real and different state from four zeroes: it means the AI has
 * not processed a message for this lead yet. The screen says so rather than rendering four
 * empty bars that look like a score of zero.
 */
export function LeadDetail({ leadId }: { leadId: string }) {
  const lead = useQuery(leadOptions(leadId));
  // The conversation read is filtered server-side to this lead. The previous version fetched
  // the whole inbox and picked the matching row in the browser, which meant the lead page
  // was only correct for a lead inside the first page of threads.
  const conversations = useQuery({
    ...conversationsOptions(leadId),
    enabled: Boolean(lead.data?.id),
  });
  const handoffs = useQuery({
    ...handoffsOptions(),
    enabled: Boolean(lead.data?.id),
  });

  if (lead.isPending) return <DetailSkeleton />;
  if (lead.isError) {
    return (
      <div className="mx-auto w-full max-w-3xl px-4 py-8 lg:px-6">
        <ErrorState
          title="Could not load this lead"
          body="The API did not respond. Nothing has been lost."
          onRetry={() => void lead.refetch()}
        />
      </div>
    );
  }

  const value = lead.data;
  const stage = leadStage(value.status);
  const thread = conversations.data?.items[0];
  const handoff = handoffs.data?.items.find(
    (h) => h.lead_id === value.id && h.status !== "RESOLVED" && h.status !== "CANCELLED",
  );

  return (
    <div className="mx-auto w-full max-w-3xl px-4 py-6 lg:px-6 lg:py-8">
      <Button asChild variant="ghost" size="sm" className="-ml-2 mb-4">
        <Link href="/leads">
          <ArrowLeft aria-hidden className="size-3.5" />
          Leads
        </Link>
      </Button>

      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h1 className="truncate text-2xl font-semibold tracking-tight">
            {displayName(value.name)}
          </h1>
          <p className="mt-0.5 text-sm text-muted-foreground">
            {[value.email, value.phone].filter(Boolean).join(" · ") || "No contact details"}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge tone={stage.tone} title={stage.blurb}>
            {stage.label}
          </StatusBadge>
          {handoff && <StatusBadge tone={handoffReason(handoff.reason).tone}>Needs a person</StatusBadge>}
        </div>
      </header>

      <section aria-labelledby="qual-heading" className="mt-8">
        <h2 id="qual-heading" className="text-sm font-semibold tracking-wide uppercase">
          How the AI scored them
        </h2>
        <div className="mt-3">
          {value.qualification ? (
            <ul className="flex flex-col gap-3 rounded-lg border p-4">
              {qualificationDimensions.map((dimension) => {
                const score = value.qualification?.[dimension.key] ?? 0;
                const percent = Math.min(100, (score / DIMENSION_MAX) * 100);
                return (
                  <li key={dimension.key} className="flex items-center gap-3">
                    <span className="w-20 shrink-0 text-xs text-muted-foreground">
                      {dimension.label}
                    </span>
                    <span className="h-2 flex-1 overflow-hidden rounded-full bg-muted">
                      <span
                        className="block h-full rounded-full bg-sky-500"
                        style={{ width: `${percent}%` }}
                      />
                    </span>
                    <span className="w-14 shrink-0 text-right text-xs tabular-nums">
                      {score}/{DIMENSION_MAX}
                    </span>
                  </li>
                );
              })}
              {value.qualification.total !== undefined && (
                <li className="mt-1 flex items-center justify-between border-t pt-3 text-sm">
                  <span className="font-medium">Total</span>
                  <span className="font-semibold tabular-nums">
                    {value.qualification.total}/100
                  </span>
                </li>
              )}
            </ul>
          ) : (
            <EmptyState
              title="Not scored yet"
              body="The AI scores a lead after its first message. This person has not been through the AI yet."
            />
          )}
        </div>
      </section>

      <section aria-labelledby="next-heading" className="mt-8">
        <h2 id="next-heading" className="text-sm font-semibold tracking-wide uppercase">
          Next action
        </h2>
        <div className="mt-3 flex flex-wrap gap-2">
          {thread ? (
            <Button asChild size="sm">
              <a href={`/conversations/${thread.id}`}>
                <MessageSquare aria-hidden className="size-4" />
                Open conversation
              </a>
            </Button>
          ) : (
            <p className="text-sm text-muted-foreground">
              No conversation yet. One starts when this person messages your Page.
            </p>
          )}
          {handoff && (
            <Button asChild size="sm" variant="outline">
              <a href={`/handoffs?focus=${handoff.id}`}>Open handoff</a>
            </Button>
          )}
        </div>
        {handoff && (
          <p className="mt-2 text-sm text-muted-foreground">
            {handoffReason(handoff.reason).blurb} Waiting since{" "}
            {relativeTime(handoff.created_at)}.
          </p>
        )}
      </section>

      <section aria-labelledby="detail-heading" className="mt-8">
        <h2 id="detail-heading" className="text-sm font-semibold tracking-wide uppercase">
          Details
        </h2>
        <dl className="mt-3 divide-y overflow-hidden rounded-lg border">
          <Row label="Came from" value="Meta lead ad" />
          <Row label="Form submission id" value={<span className="font-mono text-xs">{value.meta_leadgen_id}</span>} />
          {value.ad_id && <Row label="Ad" value={<span className="font-mono text-xs">{value.ad_id}</span>} />}
          <Row label="First seen" value={absoluteTime(value.created_at)} title={absoluteTime(value.created_at)} />
          <Row label="Last updated" value={relativeTime(value.updated_at)} title={absoluteTime(value.updated_at)} />
        </dl>
        {Object.keys(value.field_data).length > 0 && (
          <details className="mt-3 rounded-lg border">
            <summary className="cursor-pointer px-4 py-3 text-sm font-medium">
              What they told you on the form
            </summary>
            <dl className="divide-y border-t">
              {Object.entries(value.field_data).map(([key, item]) => (
                <div key={key} className="flex gap-4 px-4 py-2 text-sm">
                  <dt className="w-40 shrink-0 text-muted-foreground">{key}</dt>
                  <dd className="min-w-0 break-words">{Array.isArray(item) ? item.join(", ") : String(item)}</dd>
                </div>
              ))}
            </dl>
          </details>
        )}
      </section>
    </div>
  );
}

function Row({ label, value, title }: { label: string; value: React.ReactNode; title?: string }) {
  return (
    <div className="flex gap-4 px-4 py-2.5 text-sm">
      <dt className="w-44 shrink-0 text-muted-foreground">{label}</dt>
      <dd className="min-w-0 break-words" title={title}>
        {value}
      </dd>
    </div>
  );
}

function DetailSkeleton() {
  return (
    <div className="mx-auto w-full max-w-3xl px-4 py-6 lg:px-6 lg:py-8">
      <LoadingRegion label="Loading lead">
        <Skeleton className="h-8 w-20" />
        <Skeleton className="mt-6 h-8 w-64" />
        <Skeleton className="mt-8 h-40 w-full rounded-lg" />
        <Skeleton className="mt-6 h-12 w-48 rounded-lg" />
      </LoadingRegion>
    </div>
  );
}