import type { Metadata } from "next";

import { LeadDetail } from "@/components/leads/lead-detail";

export const metadata: Metadata = { title: "Lead · QualiFly" };

/**
 * `params` is a Promise in this Next version - the generated route types make that explicit -
 * so the page is async and awaits it. The id is then handed to the client view, which owns
 * the query.
 */
export default async function LeadPage({ params }: PageProps<"/leads/[id]">) {
  const { id } = await params;
  return <LeadDetail leadId={id} />;
}
