"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { ArrowRight, Search, Users } from "lucide-react";

import { Button } from "@/components/ui/button";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { LoadingRegion, Skeleton } from "@/components/ui/skeleton";
import { StatusBadge } from "@/components/ui/status-badge";
import { leadsOptions, type Lead, type LeadStatus } from "@/lib/api/leads";
import { displayName, leadStage, relativeTime } from "@/lib/format";

/**
 * The leads list.
 *
 * A working list, not a dump. Three things make it one:
 *
 * * **Search on what a person knows.** Name, email or phone - the three things someone
 *   actually has when they call asking about a lead. Debounced at 300ms so typing "Priya"
 *   is four requests and not one per keystroke.
 * * **Status from the URL.** The dashboard links to `/leads?status=NEW`, so the filter is
 *   shareable and the back button works. State in the URL rather than in `useState` is the
 *   difference between a screen you can hand to a colleague and one you cannot.
 * * **Every row says what to do next.** A lead with a human handoff links to its
 *   conversation, because the action for a hot lead is talking to them.
 */
const STATUSES: { value: LeadStatus | "all"; label: string }[] = [
  { value: "all", label: "All" },
  { value: "NEW", label: "New" },
  { value: "QUALIFIED", label: "Qualified" },
  { value: "HUMAN_HANDOFF", label: "Needs a person" },
  { value: "REPLIED", label: "Nurturing" },
  { value: "CLOSED", label: "Closed" },
];

const PAGE_SIZE = 25;
const DEBOUNCE_MS = 300;

export function LeadsView() {
  const params = useSearchParams();
  const router = useRouter();
  const statusParam = (params.get("status") ?? "all") as LeadStatus | "all";
  const [searchInput, setSearchInput] = useState("");
  const [search, setSearch] = useState("");
  const [offset, setOffset] = useState(0);

  // Debounce. The query key changes on the debounced value, not on every keystroke, so the
  // cache does not fill with a row per character typed.
  useEffect(() => {
    const timer = setTimeout(() => {
      setSearch(searchInput.trim());
      setOffset(0);
    }, DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [searchInput]);

  /** The filter lives in the URL, so the view is shareable and the back button works. */
  function setStatus(status: string) {
    const params = new URLSearchParams(window.location.search);
    if (status === "all") params.delete("status");
    else params.set("status", status);
    const query = params.toString();
    router.replace(query ? `/leads?${query}` : "/leads");
    setOffset(0);
  }

  const query = useQuery(leadsOptions({ status: statusParam === "all" ? undefined : statusParam, search, limit: PAGE_SIZE, offset }));

  return (
    <div className="mx-auto w-full max-w-5xl px-4 py-6 lg:px-6 lg:py-8">
      <header className="flex flex-col gap-1">
        <h1 className="text-2xl font-semibold tracking-tight">Leads</h1>
        <p className="text-sm text-muted-foreground">
          Everyone who filled in one of your forms. Search by name, email or phone.
        </p>
      </header>

      <div className="mt-5 flex flex-col gap-3">
        <div className="relative">
          <Search
            aria-hidden
            className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground"
          />
          <label htmlFor="lead-search" className="sr-only">
            Search leads
          </label>
          <input
            id="lead-search"
            value={searchInput}
            onChange={(event) => setSearchInput(event.target.value)}
            placeholder="Search by name, email or phone…"
            className="h-10 w-full rounded-lg border bg-background pr-3 pl-9 text-sm outline-none placeholder:text-muted-foreground focus-visible:ring-2 focus-visible:ring-ring"
          />
        </div>

        <div className="flex flex-wrap gap-1.5" role="group" aria-label="Filter by stage">
          {STATUSES.map((option) => (
            <FilterPill
              key={option.value}
              label={option.label}
              active={statusParam === option.value}
              onClick={() => setStatus(option.value)}
            />
          ))}
        </div>
      </div>

      <div className="mt-5">
        {query.isPending ? (
          <LoadingRegion label="Loading leads">
            <div className="flex flex-col gap-2">
              {Array.from({ length: 6 }, (_, i) => (
                <Skeleton key={i} className="h-16 w-full rounded-lg" />
              ))}
            </div>
          </LoadingRegion>
        ) : query.isError ? (
          <ErrorState
            title="Could not load your leads"
            body="The API did not respond. Your leads are safe - try again in a moment."
            onRetry={() => void query.refetch()}
          />
        ) : query.data.items.length === 0 ? (
          <EmptyState
            icon={Users}
            title={search ? `No lead matches “${search}”` : "No leads yet"}
            body={
              search
                ? "Try a different name, email or phone number."
                : "Leads appear the moment someone fills in one of your Meta lead forms. Connect your Page to start collecting them."
            }
            action={search ? undefined : { label: "Connect Meta", href: "/settings?tab=meta" }}
            secondaryAction={search ? { label: "Clear search", onClick: () => setSearchInput("") } : undefined}
          />
        ) : (
          <>
            <ul className="divide-y overflow-hidden rounded-lg border">
              {query.data.items.map((lead) => (
                <LeadRow key={lead.id} lead={lead} />
              ))}
            </ul>
            <Pagination
              offset={offset}
              shown={query.data.items.length}
              pageSize={PAGE_SIZE}
              total={query.data.total ?? query.data.items.length}
              onChange={setOffset}
            />
          </>
        )}
      </div>
    </div>
  );
}

function LeadRow({ lead }: { lead: Lead }) {
  const stage = leadStage(lead.status);
  return (
    <li>
      <Link
        href={`/leads/${lead.id}`}
        className="flex flex-wrap items-center gap-x-4 gap-y-2 p-3 transition-colors hover:bg-accent/40 sm:flex-nowrap"
      >
        <span className="flex min-w-0 flex-1 flex-col">
          <span className="flex flex-wrap items-center gap-2">
            <span className="truncate text-sm font-medium">{displayName(lead.name)}</span>
            <StatusBadge tone={stage.tone}>{stage.label}</StatusBadge>
            {lead.qualification?.high_intent && <StatusBadge tone="warning">High intent</StatusBadge>}
          </span>
          <span className="mt-0.5 truncate text-xs text-muted-foreground">
            {lead.email ?? lead.phone ?? "No contact details captured"}
          </span>
        </span>
        <span className="shrink-0 text-xs whitespace-nowrap text-muted-foreground">
          {relativeTime(lead.created_at)}
        </span>
        <ArrowRight aria-hidden className="size-3.5 shrink-0 text-muted-foreground" />
      </Link>
    </li>
  );
}

function FilterPill({
  label,
  active,
  onClick,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={
        active
          ? "rounded-full border px-3 py-1 text-xs font-medium"
          : "rounded-full border border-muted px-3 py-1 text-xs text-muted-foreground hover:bg-accent"
      }
    >
      {label}
    </button>
  );
}

function Pagination({
  offset,
  shown,
  pageSize,
  total,
  onChange,
}: {
  offset: number;
  /** Rows actually on this page, not the page size. */
  shown: number;
  pageSize: number;
  total: number;
  onChange: (next: number) => void;
}) {
  if (total <= pageSize) return null;
  // `shown`, not `pageSize`: the last page is usually short, and claiming "50–75 of 60"
  // when three rows are on screen is the kind of small lie that makes people stop
  // believing a count.
  const from = offset + 1;
  const to = offset + shown;
  return (
    <nav aria-label="Pagination" className="mt-4 flex items-center justify-between gap-3">
      <p className="text-xs text-muted-foreground">
        Showing {from}–{to} of {total}
      </p>
      <div className="flex gap-2">
        <Button
          size="sm"
          variant="outline"
          disabled={offset === 0}
          onClick={() => onChange(Math.max(0, offset - pageSize))}
        >
          Previous
        </Button>
        <Button
          size="sm"
          variant="outline"
          disabled={to >= total}
          onClick={() => onChange(offset + pageSize)}
        >
          Next
        </Button>
      </div>
    </nav>
  );
}
