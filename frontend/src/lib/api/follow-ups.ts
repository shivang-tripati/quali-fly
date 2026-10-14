import { queryOptions } from "@tanstack/react-query";
import { z } from "zod";

import { apiRequest } from "@/lib/api/client";

/**
 * Follow-up tasks, translated.
 *
 * Phase 7's endpoint is a `scheduled_tasks` read and it stays that way - the UI does not get
 * its own endpoint, and no scheduler behaviour changes. What this module adds is the one
 * thing the raw rows cannot provide: **words**.
 *
 * `task_type` of `FOLLOW_UP` is "Follow-up #1". `GHOST` is not a task a person acts on, so
 * `taskKindLabel` renders it as the lead's state rather than a scheduler phase. Putting that
 * mapping here rather than in the component is what makes it a single decision: today the
 * follow-ups screen asks for the translation in one place, and if another screen ever needs
 * it, it asks this module instead of re-deciding what a `sequence_step` means.
 */
const taskSchema = z.object({
  id: z.string().uuid(),
  conversation_id: z.string().uuid(),
  lead_id: z.string().uuid().nullable(),
  task_type: z.enum(["FOLLOW_UP", "GHOST"]),
  sequence_step: z.number(),
  status: z.enum(["PENDING", "PROCESSING", "COMPLETED", "FAILED", "CANCELLED"]),
  scheduled_at: z.string(),
  attempt_count: z.number(),
  max_attempts: z.number(),
  last_error: z.string().nullable(),
  ai_enhanced: z.boolean(),
  sent_text: z.string().nullable(),
  completed_at: z.string().nullable(),
  cancelled_at: z.string().nullable(),
  created_at: z.string(),
  updated_at: z.string(),
});

const taskListSchema = z.object({
  items: z.array(taskSchema),
  count: z.number(),
  summary: z.record(z.string(), z.number()).optional(),
});

export type ScheduledTask = z.infer<typeof taskSchema>;

export const followUpKeys = { all: ["follow-ups"] as const };

export function scheduledTasksOptions() {
  return queryOptions({
    queryKey: followUpKeys.all,
    queryFn: () => apiRequest("/api/v1/scheduled-tasks?limit=200", taskListSchema),
    // Automation state changes on the order of minutes, and the screen is a checklist
    // rather than something someone watches. A short stale time avoids refetching on every
    // focus while still picking up a task that has just fired.
    staleTime: 30_000,
  });
}

/** "Follow-up #2" for a sequence step; a lead's state for the internal ghost evaluation. */
export function taskKindLabel(task: ScheduledTask): string {
  if (task.task_type === "GHOST") return "Lead has gone quiet";
  return `Follow-up #${task.sequence_step}`;
}
