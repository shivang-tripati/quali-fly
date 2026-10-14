"use client";

import { useState } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { Inbox, MessageCircleOff, Send } from "lucide-react";

import { Button } from "@/components/ui/button";
import { EmptyState, ErrorState, InlineError } from "@/components/ui/states";
import { LoadingRegion, Skeleton } from "@/components/ui/skeleton";
import { StatusBadge } from "@/components/ui/status-badge";
import {
  conversationOptions,
  conversationsOptions,
  messagesOptions,
  sendFailureLabels,
  useSendMessage,
  type Conversation,
  type Message,
} from "@/lib/api/meta";
import { handoffsOptions } from "@/lib/api/handoffs";
import { channelLabels, relativeTime } from "@/lib/format";

/**
 * The operational inbox.
 *
 * An inbox, not a chat client. Three choices keep it that way:
 *
 * * **Two panes on desktop, one on mobile.** A list that is 240px next to a thread is how an
 *   inbox stays usable; the same two panes on a 390px screen is 150px of each, so below
 *   `md` the thread takes the full width and the list becomes a back button.
 * * **The window state is honest.** Meta refuses a plain reply 24 hours after the last
 *   inbound message. Rather than letting someone compose, send, and be refused, the composer
 *   disables itself and says why - and names the fact rather than showing a dead input.
 * * **A human handoff is visible on the thread.** If the AI has stood down for this
 *   conversation, that is the most important thing on the screen and it says so above the
 *   messages.
 */
export function ConversationsView({ selectedId }: { selectedId?: string }) {
  const conversations = useQuery(conversationsOptions());
  const handoffs = useQuery(handoffsOptions());
  // A deep link can name a thread that is not in the first page of the list, so the thread
  // the URL asks for is fetched by id. Without this the view would quietly show the newest
  // thread while the address bar names a different one.
  const deepLink = useQuery({
    ...conversationOptions(selectedId ?? ""),
    enabled: Boolean(selectedId) && !conversations.data?.items.some((c) => c.id === selectedId),
  });

  if (conversations.isPending) return <InboxSkeleton />;
  if (conversations.isError) {
    return (
      <div className="mx-auto w-full max-w-5xl px-4 py-8 lg:px-6">
        <ErrorState
          title="Could not load conversations"
          body="The API did not respond. No messages have been lost."
          onRetry={() => void conversations.refetch()}
        />
      </div>
    );
  }

  const listed = conversations.data.items;
  // The named thread is shown whether or not it made the list page, and a link to a thread
  // this tenant cannot see is a 404 rather than a silent substitution.
  const deepLinked = deepLink.data ?? null;
  const items =
    deepLinked && !listed.some((c) => c.id === deepLinked.id) ? [deepLinked, ...listed] : listed;

  if (items.length === 0) {
    return (
      <div className="mx-auto w-full max-w-5xl px-4 py-8 lg:px-6">
        <EmptyState
          icon={Inbox}
          title="No conversations yet"
          body="A thread appears as soon as someone messages your connected Page or Instagram account. Nothing else needs configuring."
          action={{ label: "Connect Meta", href: "/settings?tab=meta" }}
        />
      </div>
    );
  }

  const selected =
    items.find((c) => c.id === selectedId) ??
    (selectedId && deepLink.isError
      ? null
      : (items[0] as (typeof items)[number]));
  const handoffByConversation = new Map(
    (handoffs.data?.items ?? [])
      .filter((h) => h.status !== "RESOLVED" && h.status !== "CANCELLED")
      .map((h) => [h.conversation_id, h]),
  );

  if (!selected) {
    return (
      <div className="mx-auto w-full max-w-5xl px-4 py-8 lg:px-6">
        <EmptyState
          icon={MessageCircleOff}
          title="That conversation is not available"
          body="It may have been removed, or it belongs to another workspace. Nothing has been lost on your side."
          action={{ label: "Back to the inbox", href: "/conversations" }}
        />
      </div>
    );
  }

  return (
    <div className="mx-auto w-full max-w-5xl px-4 py-6 lg:px-6 lg:py-8">
      <header className="mb-5 flex flex-col gap-1">
        <h1 className="text-2xl font-semibold tracking-tight">Conversations</h1>
        <p className="text-sm text-muted-foreground">
          {items.length} thread{items.length === 1 ? "" : "s"}, most recent first.
        </p>
      </header>

      <div className="grid gap-4 md:grid-cols-[minmax(0,17rem)_minmax(0,1fr)]">
        {/* The list. On mobile it only shows when no thread is deep-linked, so the thread is
            never squeezed into half a phone screen. */}
        <div className={selectedId ? "hidden md:block" : "block"}>
          <ul className="divide-y overflow-hidden rounded-lg border">
            {items.map((conversation) => {
              const active = conversation.id === selected.id;
              return (
                <li key={conversation.id}>
                  <Link
                    href={`/conversations/${conversation.id}`}
                    aria-current={active ? "true" : undefined}
                    className={
                      active
                        ? "flex flex-col gap-1 p-3 bg-accent/60"
                        : "flex flex-col gap-1 p-3 transition-colors hover:bg-accent/40"
                    }
                  >
                    <span className="flex items-center gap-2">
                      <span className="truncate text-sm font-medium">
                        {conversation.lead_name ?? conversation.meta_participant_id}
                      </span>
                      {handoffByConversation.has(conversation.id) && (
                        <StatusBadge tone="warning">Person</StatusBadge>
                      )}
                      {!conversation.within_messaging_window && (
                        <StatusBadge tone="neutral" title="Meta will refuse a plain reply outside 24 hours">
                          Closed
                        </StatusBadge>
                      )}
                    </span>
                    <span className="truncate text-xs text-muted-foreground">
                      {channelLabels[conversation.channel]} ·{" "}
                      {relativeTime(conversation.last_message_at)}
                    </span>
                  </Link>
                </li>
              );
            })}
          </ul>
        </div>

        <ConversationThread
          conversation={selected}
          handoff={handoffByConversation.get(selected.id)}
        />
      </div>
    </div>
  );
}

function ConversationThread({
  conversation,
  handoff,
}: {
  conversation: Conversation;
  handoff?: { id: string; reason: string };
}) {
  const messages = useQuery(messagesOptions(conversation.id));
  const send = useSendMessage(conversation.id);
  const [draft, setDraft] = useState("");
  const canReply = conversation.within_messaging_window;

  return (
    <div className="flex min-w-0 flex-col rounded-lg border">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b p-3">
        <div className="min-w-0">
          <p className="truncate text-sm font-medium">
            {conversation.lead_name ?? conversation.meta_participant_id}
          </p>
          <p className="truncate text-xs text-muted-foreground">
            {channelLabels[conversation.channel]}
            {conversation.lead_name ? ` · ${conversation.meta_participant_id}` : ""}
          </p>
          <p className="text-xs text-muted-foreground">
            {canReply
              ? "A reply is accepted until " +
                (conversation.messaging_window_expires_at
                  ? new Date(conversation.messaging_window_expires_at).toLocaleString()
                  : "the window closes")
              : "Meta's 24-hour window has closed, so a plain reply will be refused."}
          </p>
        </div>
        {handoff && (
          <Button asChild size="sm" variant="outline">
            <a href={`/handoffs?focus=${handoff.id}`}>Needs a person</a>
          </Button>
        )}
      </div>

      <div className="flex min-h-64 flex-col gap-3 p-3">
        {messages.isPending && (
          <LoadingRegion label="Loading messages">
            <div className="flex flex-col gap-2">
              {Array.from({ length: 4 }, (_, i) => (
                <Skeleton key={i} className="h-12 w-2/3 rounded-lg" />
              ))}
            </div>
          </LoadingRegion>
        )}
        {messages.isError && (
          <p className="text-sm text-destructive">
            Could not load this conversation&apos;s messages. Refresh to try again.
          </p>
        )}
        {messages.data?.count === 0 && (
          <p className="text-sm text-muted-foreground">No messages in this thread yet.</p>
        )}
        <ul className="flex flex-col gap-2">
          {messages.data?.items.map((message) => (
            <MessageRow key={message.id} message={message} />
          ))}
        </ul>
      </div>

      <form
        className="flex flex-col gap-2 border-t p-3"
        onSubmit={(event) => {
          event.preventDefault();
          const text = draft.trim();
          if (!text) return;
          setDraft("");
          send.mutate(text);
        }}
      >
        <label htmlFor="composer" className="text-xs text-muted-foreground">
          {canReply ? "Reply" : "Replies are disabled outside the 24-hour window"}
        </label>
        <textarea
          id="composer"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          disabled={!canReply || send.isPending}
          placeholder={canReply ? "Write a reply…" : "Meta will not accept a reply right now."}
          className="min-h-20 rounded-md border p-2 text-sm disabled:cursor-not-allowed disabled:bg-muted/50"
        />
        <div className="flex items-center gap-3">
          <Button type="submit" size="sm" disabled={!canReply || send.isPending || draft.trim().length === 0}>
            <Send aria-hidden className="size-3.5" />
            {send.isPending ? "Sending…" : "Send"}
          </Button>
          {send.isError && <InlineError error={send.error} onRetry={() => send.reset()} />}
        </div>
      </form>
    </div>
  );
}

function MessageRow({ message }: { message: Message }) {
  const inbound = message.direction === "INBOUND";
  // Who wrote it, in the user's terms: "You" for a human, "AI" only when the row really is
  // AI-attributed. The API sets `ai_model` on the AI's own sends, so this is a fact rather
  // than a guess about tone.
  const attribution = inbound ? "Lead" : message.ai_model ? "AI" : "You";
  return (
    <li className="flex flex-col gap-1">
      <div className={inbound ? "self-start" : "self-end"}>
        <span
          className={
            inbound
              ? "block max-w-md rounded-lg border p-2 text-sm"
              : "block max-w-md rounded-lg border bg-accent p-2 text-sm"
          }
        >
          {message.text ?? `[${message.message_type}]`}
        </span>
      </div>
      <div
        className={`flex items-center gap-2 text-xs text-muted-foreground ${inbound ? "" : "self-end"}`}
      >
        <span>{attribution}</span>
        <span>·</span>
        <span>{relativeTime(message.sent_at)}</span>
        {!inbound && message.delivery !== "SENT" && (
          <StatusBadge tone="neutral">
            {message.delivery === "PENDING"
              ? "Sending"
              : (sendFailureLabels[message.failure_reason ?? ""] ?? "Not sent")}
          </StatusBadge>
        )}
      </div>
    </li>
  );
}

function InboxSkeleton() {
  return (
    <div className="mx-auto w-full max-w-5xl px-4 py-6 lg:px-6 lg:py-8">
      <LoadingRegion label="Loading conversations">
        <Skeleton className="h-8 w-48" />
        <div className="mt-5 grid gap-4 md:grid-cols-[minmax(0,17rem)_minmax(0,1fr)]">
          <Skeleton className="h-72 w-full rounded-lg" />
          <Skeleton className="h-72 w-full rounded-lg" />
        </div>
      </LoadingRegion>
    </div>
  );
}