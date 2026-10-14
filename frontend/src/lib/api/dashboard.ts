import { queryOptions } from "@tanstack/react-query";
import { z } from "zod";

import { apiRequest } from "@/lib/api/client";

/**
 * The dashboard read model.
 *
 * Every field here was chosen to be renderable without a second lookup: an attention item
 * carries its own title, its own sentence of context and its own age, so the queue renders
 * from one response with no N+1 and no cross-referencing. That is the reason the backend
 * translates its internals before they leave the service - if this client had to build
 * "waiting 18m" from a `created_at` and a lead name it would have to join three tables in
 * the browser.
 *
 * There is deliberately no `tenant_id` in the request. The tenant comes from the session.
 */
const attentionItemSchema = z.object({
  kind: z.enum([
    "handoff",
    "follow_up_overdue",
    "follow_up_due",
    "ai_failure",
    "message_failed",
    "connection",
    "knowledge",
  ]),
  severity: z.enum(["critical", "warning", "info"]),
  title: z.string(),
  detail: z.string(),
  waiting_seconds: z.number(),
  conversation_id: z.string().uuid().nullable(),
  handoff_id: z.string().uuid().nullable(),
  lead_id: z.string().uuid().nullable(),
  label: z.string().nullable(),
});

const summarySchema = z.object({
  new_leads: z.number(),
  new_leads_today: z.number(),
  qualified_leads: z.number(),
  handoffs_open: z.number(),
  handoffs_unassigned: z.number(),
  handoffs_mine: z.number(),
  follow_ups_due: z.number(),
  follow_ups_overdue: z.number(),
  follow_ups_upcoming: z.number(),
  knowledge_documents: z.number(),
});

const leadActivitySchema = z.object({
  new: z.number(),
  qualified: z.number(),
  replying: z.number(),
  handoff: z.number(),
  closed: z.number(),
});

const recentConversationSchema = z.object({
  id: z.string().uuid(),
  lead_id: z.string().uuid().nullable(),
  lead_name: z.string().nullable(),
  lead_status: z.string().nullable(),
  channel: z.string(),
  last_message_text: z.string().nullable(),
  last_message_direction: z.enum(["INBOUND", "OUTBOUND"]).nullable(),
  last_message_delivery: z.string().nullable(),
  last_message_at: z.string().nullable(),
  has_active_handoff: z.boolean(),
  within_messaging_window: z.boolean(),
});

const integrationSchema = z.object({
  meta_connected: z.boolean(),
  meta_needs_attention: z.boolean(),
  meta_connection_count: z.number(),
  last_webhook_at: z.string().nullable(),
  knowledge_documents: z.number(),
  knowledge_chunks: z.number(),
});

const dashboardSchema = z.object({
  summary: summarySchema,
  attention: z.array(attentionItemSchema),
  lead_activity: leadActivitySchema,
  recent_conversations: z.array(recentConversationSchema),
  // Null for a MEMBER: the backend omits workspace health for anyone who does not own it,
  // and the UI has to not render an empty "automation health" panel at them.
  integration: integrationSchema.nullable(),
  generated_at: z.string(),
});

export type AttentionItem = z.infer<typeof attentionItemSchema>;
export type Dashboard = z.infer<typeof dashboardSchema>;
export type RecentConversation = z.infer<typeof recentConversationSchema>;
export type SummaryCounts = z.infer<typeof summarySchema>;

export const dashboardKeys = { all: ["dashboard"] as const };

/**
 * `staleTime` is longer than the 30s provider default on purpose.
 *
 * The dashboard is the screen a user opens first thing in the morning and then leaves; a
 * 30-second refetch storm on a page nobody is watching is wasted requests for numbers that
 * change on the order of minutes. A handoff claimed in the handoff queue invalidates this
 * key explicitly, so the number is never stale where it matters.
 */
export function dashboardOptions() {
  return queryOptions({
    queryKey: dashboardKeys.all,
    queryFn: () => apiRequest("/api/v1/dashboard", dashboardSchema),
    staleTime: 60_000,
  });
}

/** Where an attention item takes you. The single mapping from kind to destination. */
export function attentionHref(item: AttentionItem): string | null {
  switch (item.kind) {
    case "handoff":
      return item.handoff_id ? `/handoffs?focus=${item.handoff_id}` : "/handoffs";
    case "follow_up_overdue":
    case "follow_up_due":
    case "ai_failure":
    case "message_failed":
      return item.conversation_id ? `/conversations/${item.conversation_id}` : "/conversations";
    case "connection":
      return "/settings?tab=meta";
    case "knowledge":
      return "/knowledge";
  }
}

/** The verb on the attention item's button. Named per kind so no screen hard-codes it. */
export function attentionAction(item: AttentionItem): string {
  switch (item.kind) {
    case "handoff":
      return item.label === "UNCLAIMED" ? "Claim" : "Open";
    case "follow_up_overdue":
      return "Open conversation";
    case "follow_up_due":
      return "View";
    case "ai_failure":
    case "message_failed":
      return "Review";
    case "connection":
      return "Reconnect";
    case "knowledge":
      return "Add knowledge";
  }
}
