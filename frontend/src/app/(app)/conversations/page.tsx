import type { Metadata } from "next";

import { ConversationsView } from "@/components/conversations/conversations-view";

export const metadata: Metadata = { title: "Conversations · QualiFly" };

/**
 * The inbox with no thread selected.
 *
 * The view falls back to the most recent thread when nothing is chosen, so this route is a
 * usable inbox rather than a dead end - someone following a link to `/conversations` sees
 * the newest conversation immediately instead of an empty right-hand pane.
 */
export default function ConversationsPage() {
  return <ConversationsView />;
}
