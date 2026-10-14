"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { ArrowLeft } from "lucide-react";

import {
  knowledgeDocumentOptions,
  sourceTypeLabels,
  useDeleteKnowledge,
} from "@/lib/api/knowledge";
import { Button } from "@/components/ui/button";
import { ErrorState, InlineError } from "@/components/ui/states";
import { LoadingRegion, Skeleton } from "@/components/ui/skeleton";
import { StatusBadge } from "@/components/ui/status-badge";
import { absoluteTime, relativeTime } from "@/lib/format";

/**
 * One knowledge document, readable.
 *
 * Read-only on purpose. `PUT` on this endpoint re-chunks and re-embeds the whole text, so an
 * "edit" affordance here would need the full original text to round-trip correctly - and a
 * partial edit that silently re-chunked a document into something different from what the
 * author typed is worse than asking them to paste it again. The knowledge screen is where
 * writing happens.
 *
 * A client component because it uses hooks. The route beside it stays a server component,
 * so only this component hydrates.
 */
export function KnowledgeDocumentView({ documentId }: { documentId: string }) {
  const query = useQuery(knowledgeDocumentOptions(documentId));
  const remove = useDeleteKnowledge();

  if (query.isPending) {
    return (
      <div className="mx-auto w-full max-w-3xl px-4 py-6 lg:px-6">
        <LoadingRegion label="Loading document">
          <Skeleton className="h-8 w-64" />
          <Skeleton className="mt-6 h-96 w-full rounded-lg" />
        </LoadingRegion>
      </div>
    );
  }

  if (query.isError) {
    return (
      <div className="mx-auto w-full max-w-3xl px-4 py-8 lg:px-6">
        <ErrorState
          title="Could not open this document"
          body="It may have been deleted, or the API did not respond."
          onRetry={() => void query.refetch()}
        />
      </div>
    );
  }

  const doc = query.data;

  return (
    <div className="mx-auto w-full max-w-3xl px-4 py-6 lg:px-6 lg:py-8">
      <Button asChild variant="ghost" size="sm" className="-ml-2 mb-4">
        <Link href="/knowledge">
          <ArrowLeft aria-hidden className="size-3.5" />
          Knowledge
        </Link>
      </Button>

      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-2xl font-semibold tracking-tight">{doc.title}</h1>
          <p className="mt-1 flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
            <StatusBadge tone="info">
              {sourceTypeLabels[doc.source_type] ?? doc.source_type}
            </StatusBadge>
            <span>
              {doc.chunk_count} {doc.chunk_count === 1 ? "piece" : "pieces"} · updated{" "}
              {relativeTime(doc.updated_at)}
            </span>
          </p>
        </div>
        <Button
          size="sm"
          variant="outline"
          disabled={remove.isPending}
          onClick={() => remove.mutate(doc.document_id)}
        >
          Delete
        </Button>
      </header>

      {remove.isError && (
        <div className="mt-3">
          <InlineError error={remove.error} onRetry={() => remove.reset()} />
        </div>
      )}

      <article className="mt-6 rounded-lg border p-5">
        <p className="text-sm leading-relaxed whitespace-pre-wrap">{doc.content}</p>
      </article>

      <p className="mt-3 text-xs text-muted-foreground">
        Created {absoluteTime(doc.created_at)}. The AI searches this text when someone asks
        something it could answer from here.
      </p>
    </div>
  );
}
