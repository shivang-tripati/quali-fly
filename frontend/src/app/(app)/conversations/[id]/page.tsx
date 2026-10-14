import type { Metadata } from "next";

import { ConversationsView } from "@/components/conversations/conversations-view";

export const metadata: Metadata = { title: "Conversation · QualiFly" };

/** One thread, with the list beside it. On mobile the list gives way to the thread. */
export default async function ConversationPage({ params }: PageProps<"/conversations/[id]">) {
  const { id } = await params;
  return <ConversationsView selectedId={id} />;
}
