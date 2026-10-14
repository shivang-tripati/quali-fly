import { queryOptions, useMutation, useQueryClient } from "@tanstack/react-query";
import { z } from "zod";

import { apiRequest } from "@/lib/api/client";
import { dashboardKeys } from "@/lib/api/dashboard";

/**
 * The human handoff queue.
 *
 * Read and act on escalations: list, view, claim, start, resolve. Creation is deliberately
 * absent - a handoff is created by the AI pipeline, not by a person, and an endpoint that
 * could manufacture one would be a second, unaudited way for work to enter the queue.
 *
 * The schemas mirror `app/schemas/handoff.py` exactly. Note what is *not* here: no
 * `tenant_id` on any request. The tenant comes from the session cookie and the
 * `X-Tenant-ID` preference header, and the backend re-checks the membership either way.
 */
const handoffSchema = z.object({
  id: z.string().uuid(),
  conversation_id: z.string().uuid(),
  lead_id: z.string().uuid().nullable(),
  status: z.enum(["OPEN", "ASSIGNED", "IN_PROGRESS", "RESOLVED", "CANCELLED"]),
  reason: z.enum(["EXPLICIT_HUMAN_REQUEST", "HIGH_INTENT", "AI_FAILURE", "OTHER"]),
  detail: z.string().nullable(),
  priority: z.number(),
  assigned_user_id: z.string().uuid().nullable(),
  assigned_at: z.string().nullable(),
  acknowledged_at: z.string().nullable(),
  acknowledgement_failure: z.string().nullable(),
  acknowledgement_source: z.string().nullable(),
  resolved_at: z.string().nullable(),
  resolution_note: z.string().nullable(),
  lead_name: z.string().nullable(),
  latest_inbound_text: z.string().nullable(),
  latest_inbound_at: z.string().nullable(),
  qualification_total: z.number().nullable(),
  created_at: z.string(),
  updated_at: z.string(),
});

// Exported so a test can assert the shape directly, rather than inferring it from an
// "unexpected response shape" error that does not say which field failed.
export const handoffListSchema = z.object({
  items: z.array(handoffSchema),
  count: z.number(),
  summary: z.record(z.string(), z.number()).optional(),
});

const notificationSchema = z.object({
  id: z.string().uuid(),
  handoff_id: z.string().uuid(),
  conversation_id: z.string().uuid(),
  lead_id: z.string().uuid().nullable(),
  type: z.enum(["HANDOFF_CREATED", "HANDOFF_ACK_FAILED"]),
  title: z.string(),
  message: z.string(),
  payload: z.record(z.string(), z.unknown()),
  status: z.enum(["UNREAD", "READ"]),
  read_at: z.string().nullable(),
  created_at: z.string(),
});

const notificationListSchema = z.object({
  items: z.array(notificationSchema),
  count: z.number(),
});

export type Handoff = z.infer<typeof handoffSchema>;
export type HandoffNotification = z.infer<typeof notificationSchema>;
export type HandoffStatus = Handoff["status"];

export const handoffReasonLabels: Record<Handoff["reason"], string> = {
  EXPLICIT_HUMAN_REQUEST: "Asked for a person",
  HIGH_INTENT: "High intent",
  AI_FAILURE: "AI could not continue",
  OTHER: "Escalated",
};

export const handoffStatusLabels: Record<HandoffStatus, string> = {
  OPEN: "Open",
  ASSIGNED: "Assigned",
  IN_PROGRESS: "In progress",
  RESOLVED: "Resolved",
  CANCELLED: "Cancelled",
};

export const handoffKeys = {
  all: ["handoffs"] as const,
  list: (status?: HandoffStatus) => ["handoffs", "list", status ?? "all"] as const,
  notifications: ["handoffs", "notifications"] as const,
};

/** The queue, highest priority first. Optional status filter; the API defaults to all. */
export function handoffsOptions(status?: HandoffStatus) {
  const search = status ? `?status=${status}` : "";
  return queryOptions({
    queryKey: handoffKeys.list(status),
    queryFn: () => apiRequest(`/api/v1/handoffs${search}`, handoffListSchema),
  });
}

/** The queue filtered by lead name. Used by the command palette. */
export function handoffSearchOptions(term: string) {
  return queryOptions({
    queryKey: [...handoffKeys.list(), "search", term] as const,
    queryFn: () =>
      apiRequest(
        `/api/v1/handoffs?search=${encodeURIComponent(term)}&limit=5`,
        handoffListSchema,
      ),
  });
}

export function handoffNotificationsOptions() {
  return queryOptions({
    queryKey: handoffKeys.notifications,
    queryFn: () => apiRequest("/api/v1/handoffs/notifications/inbox", notificationListSchema),
  });
}

/**
 * One mutation factory for every workflow action.
 *
 * They all invalidate the same three things, because claiming a handoff changes the queue,
 * the notification that said someone should look at it, *and* the dashboard's counts - the
 * "3 unassigned" badge in the sidebar and the attention list are all read off the same
 * aggregate. Invalidating only the list would leave a stale "unassigned" count next to
 * work that is already owned.
 */
function useHandoffAction<TInput>(
  path: (input: TInput) => string,
  body?: (input: TInput) => Record<string, unknown> | undefined,
) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: TInput) =>
      apiRequest(path(input), handoffSchema, {
        method: "POST",
        body: JSON.stringify(body?.(input) ?? {}),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: handoffKeys.all });
      void queryClient.invalidateQueries({ queryKey: dashboardKeys.all });
    },
  });
}

export function useClaimHandoff() {
  return useHandoffAction<{ handoffId: string }>((i) => `/api/v1/handoffs/${i.handoffId}/claim`);
}

export function useStartHandoff() {
  return useHandoffAction<{ handoffId: string }>((i) => `/api/v1/handoffs/${i.handoffId}/start`);
}

export function useResolveHandoff() {
  return useHandoffAction<{ handoffId: string; note?: string }>(
    (i) => `/api/v1/handoffs/${i.handoffId}/resolve`,
    (i) => ({ note: i.note ?? null }),
  );
}

/**
 * Mark one notification read.
 *
 * The only reason this exists is the unread badge: a count that can never go down is a count
 * people learn to ignore. The backend answers 204 whether or not *this* call was the one
 * that did it, so there is no error state to handle here beyond a failed request - and a
 * failed request must not hide the row, which is why the optimistic update below only
 * settles after a successful 204.
 */
export function useMarkNotificationRead() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (notificationId: string) =>
      apiRequest(`/api/v1/handoffs/notifications/${notificationId}/read`, z.null(), {
        method: "POST",
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: handoffKeys.notifications });
    },
  });
}
