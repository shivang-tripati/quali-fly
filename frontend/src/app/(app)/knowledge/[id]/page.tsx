import type { Metadata } from "next";

import { KnowledgeDocumentView } from "@/components/knowledge/knowledge-document-view";

export const metadata: Metadata = { title: "Document · QualiFly" };

/**
 * A thin server component: it awaits the route params and hands the id to the client view.
 *
 * The view is separate because a page component in this Next version may be `async`, and
 * hooks cannot be called in an async function - so the component that uses them cannot be
 * the one that awaits the params.
 */
export default async function KnowledgeDocumentPage({ params }: PageProps<"/knowledge/[id]">) {
  const { id } = await params;
  return <KnowledgeDocumentView documentId={id} />;
}
