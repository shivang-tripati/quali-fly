"use client";

import { useState } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { BookOpen, Plus, Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { EmptyState, ErrorState, InlineError } from "@/components/ui/states";
import { LoadingRegion, Skeleton } from "@/components/ui/skeleton";
import {
  knowledgeOptions,
  sourceTypeLabels,
  useDeleteKnowledge,
  useSaveKnowledge,
  type KnowledgeSummary,
} from "@/lib/api/knowledge";
import { relativeTime } from "@/lib/format";

/**
 * "What does my AI know?"
 *
 * Framed as documents a person wrote or uploaded, not as rows. Each entry shows the title,
 * where it came from, how many pieces it was split into, and when it changed - which is what
 * someone actually needs to answer "have I added our pricing yet?".
 *
 * `chunk_count` is shown rather than hidden because it is the honest answer to "is this
 * being used?": a document that produced one piece is too short to retrieve against. A
 * count of 1 next to a long document is a prompt to make it longer, which is more useful
 * than hiding the number.
 */
export function KnowledgeView() {
  const query = useQuery(knowledgeOptions());
  const [composing, setComposing] = useState(false);

  if (query.isPending) return <KnowledgeSkeleton />;
  if (query.isError) {
    return (
      <div className="mx-auto w-full max-w-3xl px-4 py-8 lg:px-6">
        <ErrorState
          title="Could not load your knowledge"
          body="The API did not respond. Your documents are safe."
          onRetry={() => void query.refetch()}
        />
      </div>
    );
  }

  const items = query.data.items;

  return (
    <div className="mx-auto w-full max-w-3xl px-4 py-6 lg:px-6 lg:py-8">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Knowledge</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            {items.length === 0
              ? "Teach your AI what your business actually does."
              : `Your AI answers from ${items.length} document${items.length === 1 ? "" : "s"}.`}
          </p>
        </div>
        <Button size="sm" onClick={() => setComposing((c) => !c)}>
          <Plus aria-hidden className="size-4" />
          {composing ? "Cancel" : "Add knowledge"}
        </Button>
      </header>

      {composing && (
        <div className="mt-5">
          <KnowledgeForm onDone={() => setComposing(false)} />
        </div>
      )}

      <div className="mt-5">
        {items.length === 0 && !composing ? (
          <EmptyState
            icon={BookOpen}
            title="Your AI has nothing to answer from yet"
            body="Add your pricing, policies or FAQs and the AI will use them when it replies. Without this it is guessing from the message alone."
            action={{ label: "Add your first document", onClick: () => setComposing(true) }}
          />
        ) : (
          <ul className="divide-y overflow-hidden rounded-lg border">
            {items.map((item) => (
              <KnowledgeRow key={item.document_id} item={item} />
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

function KnowledgeForm({ onDone }: { onDone: () => void }) {
  const save = useSaveKnowledge();
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");

  function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!title.trim() || !content.trim()) return;
    save.mutate(
      { title: title.trim(), content },
      {
        onSuccess: () => {
          setTitle("");
          setContent("");
          onDone();
        },
      },
    );
  }

  return (
    <form onSubmit={submit} className="flex flex-col gap-3 rounded-lg border p-4">
      <div className="flex flex-col gap-1.5">
        <label htmlFor="kb-title" className="text-sm font-medium">
          Title
        </label>
        <input
          id="kb-title"
          value={title}
          onChange={(event) => setTitle(event.target.value)}
          placeholder="Pricing FAQ"
          className="h-10 rounded-md border bg-background px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />
      </div>
      <div className="flex flex-col gap-1.5">
        <label htmlFor="kb-content" className="text-sm font-medium">
          Content
        </label>
        <textarea
          id="kb-content"
          value={content}
          onChange={(event) => setContent(event.target.value)}
          placeholder="Paste your pricing, policies, FAQs…"
          className="min-h-40 rounded-md border bg-background p-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />
        <p className="text-xs text-muted-foreground">
          The text is split into pieces and indexed so the AI can find the relevant part when
          someone asks.
        </p>
      </div>
      <div className="flex items-center gap-3">
        <Button type="submit" size="sm" disabled={save.isPending || !title.trim() || !content.trim()}>
          {save.isPending ? "Saving…" : "Save"}
        </Button>
        {save.isError && <InlineError error={save.error} onRetry={() => save.reset()} />}
      </div>
    </form>
  );
}

function KnowledgeRow({ item }: { item: KnowledgeSummary }) {
  const remove = useDeleteKnowledge();

  return (
    <li className="flex flex-wrap items-center gap-x-4 gap-y-2 p-3">
      <span className="flex min-w-0 flex-1 flex-col">
        {/* The title is the way into the document. `/knowledge/[id]` and its view already
            existed, but nothing in this list pointed at it, so the one screen that shows
            what the AI actually has - and lets you delete a single document from context -
            was reachable only by typing a URL. A row you cannot open is a dead row. */}
        <Link
          href={`/knowledge/${item.document_id}`}
          className="truncate text-sm font-medium hover:underline focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
        >
          {item.title}
        </Link>
        <span className="text-xs text-muted-foreground">
          {sourceTypeLabels[item.source_type] ?? item.source_type} ·{" "}
          {item.chunk_count} {item.chunk_count === 1 ? "piece" : "pieces"} · updated{" "}
          {relativeTime(item.updated_at)}
        </span>
        {item.chunk_count === 1 && item.chunk_count > 0 && (
          <span className="text-xs text-amber-700 dark:text-amber-400">
            One piece is hard to retrieve against. Longer, sectioned text works better.
          </span>
        )}
      </span>
      <Button
        size="icon"
        variant="ghost"
        onClick={() => remove.mutate(item.document_id)}
        disabled={remove.isPending}
      >
        <Trash2 aria-hidden className="size-4" />
        <span className="sr-only">Delete {item.title}</span>
      </Button>
    </li>
  );
}

function KnowledgeSkeleton() {
  return (
    <div className="mx-auto w-full max-w-3xl px-4 py-6 lg:px-6 lg:py-8">
      <LoadingRegion label="Loading knowledge">
        <Skeleton className="h-8 w-40" />
        <div className="mt-5 flex flex-col gap-2">
          {Array.from({ length: 4 }, (_, i) => (
            <Skeleton key={i} className="h-14 w-full rounded-lg" />
          ))}
        </div>
      </LoadingRegion>
    </div>
  );
}
