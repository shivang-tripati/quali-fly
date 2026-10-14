import { queryOptions } from "@tanstack/react-query";
import { z } from "zod";

import { apiRequest } from "@/lib/api/client";

/**
 * Leads, with the filters the leads screen actually needs.
 *
 * The API also has an unfiltered `leadsOptions` in `meta.ts`, kept because the original
 * connection panel uses it. This module is the one the product uses.
 *
 * Search is debounced by the *caller*, not here. Putting the timer in the query options
 * would make the key - and therefore the cache entry - change on every keystroke regardless,
 * which fills the cache with a row per character typed.
 */

/**
 * `qualification` is the AI's merged scores. `.passthrough()` because the object is shared
 * with other phases and forbidding extras here would turn a future Phase 5 field into a
 * client crash rather than an ignored key. The four dimensions and `total` are validated
 * explicitly below, because those are what the UI renders as bars - and an unvalidated
 * score would be a bar that silently reads zero.
 */
const qualificationSchema = z
  .object({
    budget: z.number().optional(),
    timeline: z.number().optional(),
    fit: z.number().optional(),
    authority: z.number().optional(),
    total: z.number().optional(),
    high_intent: z.boolean().optional(),
  })
  .passthrough();

const leadSchema = z.object({
  id: z.string().uuid(),
  tenant_id: z.string().uuid(),
  meta_leadgen_id: z.string(),
  source: z.literal("META_LEAD_ADS"),
  status: z.enum(["NEW", "REPLIED", "QUALIFIED", "CLOSED", "GHOSTED", "HUMAN_HANDOFF"]),
  name: z.string().nullable(),
  email: z.string().nullable(),
  phone: z.string().nullable(),
  form_id: z.string().nullable(),
  ad_id: z.string().nullable(),
  provider_created_at: z.string().nullable(),
  field_data: z.record(z.string(), z.unknown()),
  qualification: qualificationSchema.nullable(),
  created_at: z.string(),
  updated_at: z.string(),
});

const leadListSchema = z.object({
  items: z.array(leadSchema),
  count: z.number(),
  total: z.number().nullable(),
});

export type Lead = z.infer<typeof leadSchema>;
export type LeadStatus = Lead["status"];

export const leadKeys = {
  all: ["leads"] as const,
  list: (status: string, search: string, offset: number) =>
    ["leads", "list", status, search, offset] as const,
  detail: (id: string) => ["leads", "detail", id] as const,
};

export interface LeadFilters {
  status?: LeadStatus;
  search?: string;
  limit?: number;
  offset?: number;
}

function leadQuery(filters: LeadFilters): string {
  const params = new URLSearchParams();
  if (filters.status) params.set("status", filters.status);
  if (filters.search) params.set("q", filters.search);
  params.set("limit", String(filters.limit ?? 25));
  params.set("offset", String(filters.offset ?? 0));
  return `/api/v1/leads?${params.toString()}`;
}

export function leadsOptions(filters: LeadFilters = {}) {
  return queryOptions({
    queryKey: leadKeys.list(
      filters.status ?? "all",
      filters.search ?? "",
      filters.offset ?? 0,
    ),
    queryFn: () => apiRequest(leadQuery(filters), leadListSchema),
  });
}

export function leadOptions(leadId: string) {
  return queryOptions({
    queryKey: leadKeys.detail(leadId),
    queryFn: () => apiRequest(`/api/v1/leads/${leadId}`, leadSchema),
  });
}
