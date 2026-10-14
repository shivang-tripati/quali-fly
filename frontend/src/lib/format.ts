/**
 * Turning backend values into words a person uses.
 *
 * Every label and every relative time in the product goes through this module, for one
 * reason: a dashboard that says "HUMAN_HANDOFF" or "8m" teaches the user to read the
 * database instead of the product. The backend deliberately speaks in database
 * vocabulary (it is an API), and this is the single place that translation happens.
 *
 * Relative time is computed here rather than with `Intl.RelativeTimeFormat` alone because
 * the thresholds are a product decision, not a locale one: "just now" up to a minute reads
 * better than "in 0 seconds", and "3 weeks ago" is more useful in an operations queue than
 * "21 days ago".
 */

const MINUTE = 60;
const HOUR = 3600;
const DAY = 86_400;

/** "just now" / "8m" / "3h" / "2d" / "5 Mar". Compact enough for a dense queue. */
export function relativeTime(value: string | Date | null | undefined): string {
  if (!value) return "—";
  const then = typeof value === "string" ? new Date(value) : value;
  const seconds = Math.max(0, Math.round((Date.now() - then.getTime()) / 1000));

  if (seconds < 45) return "just now";
  if (seconds < HOUR) return `${Math.floor(seconds / MINUTE)}m ago`;
  if (seconds < DAY) return `${Math.floor(seconds / HOUR)}h ago`;
  if (seconds < 7 * DAY) return `${Math.floor(seconds / DAY)}d ago`;
  return then.toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

/** The long form, for tooltips and detail rows where "8m ago" is too terse. */
export function absoluteTime(value: string | Date | null | undefined): string {
  if (!value) return "—";
  const date = typeof value === "string" ? new Date(value) : value;
  return date.toLocaleString(undefined, {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** How long something has been waiting, phrased for a queue item. */
export function waitingFor(seconds: number): string {
  if (seconds < 60) return "just now";
  if (seconds < HOUR) {
    const minutes = Math.floor(seconds / MINUTE);
    return `${minutes} minute${minutes === 1 ? "" : "s"}`;
  }
  if (seconds < DAY) {
    const hours = Math.floor(seconds / HOUR);
    return `${hours} hour${hours === 1 ? "" : "s"}`;
  }
  const days = Math.floor(seconds / DAY);
  return `${days} day${days === 1 ? "" : "s"}`;
}

/** "in 35 min" / "2h overdue" / "Tomorrow, 10:30" - the follow-up list's vocabulary. */
export function scheduleTime(value: string | Date | null | undefined): string {
  if (!value) return "—";
  const date = typeof value === "string" ? new Date(value) : value;
  const seconds = Math.round((date.getTime() - Date.now()) / 1000);
  if (seconds < -HOUR) return `${waitingFor(-seconds)} overdue`;
  if (seconds < 0) return "Due now";
  if (seconds < HOUR) return `in ${Math.max(1, Math.floor(seconds / MINUTE))} min`;
  if (seconds < 6 * HOUR) return `in ${Math.floor(seconds / HOUR)}h`;
  if (seconds < DAY) return "Later today";
  if (seconds < 2 * DAY) return "Tomorrow";
  return date.toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

// -- product vocabulary ------------------------------------------------------

export type LeadStage = "new" | "replied" | "qualified" | "handoff" | "closed" | "ghosted";

/**
 * Lead lifecycle in plain words.
 *
 * `tone` drives colour, and the mapping is the product's semantic system: neutral for
 * unworked, blue for the AI working on it, green for progress, amber for waiting on a
 * person, red for needing attention. Every status gets a tone whether or not it uses the
 * strong colour - an unworked lead is *not* a problem and must not look like one.
 */
export const leadStageMeta: Record<
  string,
  { label: string; tone: "neutral" | "info" | "success" | "warning" | "danger"; blurb: string }
> = {
  NEW: {
    label: "New",
    tone: "info",
    blurb: "Arrived, not yet answered.",
  },
  REPLIED: {
    label: "Replied",
    tone: "neutral",
    blurb: "Answered by the AI, still nurturing.",
  },
  QUALIFIED: {
    label: "Qualified",
    tone: "success",
    blurb: "Scored as a serious lead.",
  },
  HUMAN_HANDOFF: {
    label: "Needs a person",
    tone: "warning",
    blurb: "Escalated to your team. The AI has stood down.",
  },
  CLOSED: { label: "Closed", tone: "neutral", blurb: "Finished, one way or another." },
  GHOSTED: {
    label: "Gone quiet",
    tone: "neutral",
    blurb: "No reply for a week. Not waiting on anyone.",
  },
};

export function leadStage(status: string | null | undefined) {
  return (
    leadStageMeta[status ?? "NEW"] ?? {
      label: status ?? "Unknown",
      tone: "neutral" as const,
      blurb: "",
    }
  );
}

export const channelLabels: Record<string, string> = {
  MESSENGER: "Messenger",
  INSTAGRAM: "Instagram",
};

export const sourceLabels: Record<string, string> = {
  META_LEAD_ADS: "Meta ad",
};

export const handoffReasonMeta: Record<
  string,
  { label: string; blurb: string; tone: "neutral" | "info" | "success" | "warning" | "danger" }
> = {
  EXPLICIT_HUMAN_REQUEST: {
    label: "Asked for a person",
    blurb: "The lead asked to speak to someone.",
    tone: "danger",
  },
  HIGH_INTENT: {
    label: "High intent",
    blurb: "Scored high enough to warrant a person.",
    tone: "warning",
  },
  AI_FAILURE: {
    label: "AI could not continue",
    blurb: "Something broke, so a person should take over.",
    tone: "warning",
  },
  OTHER: { label: "Escalated", blurb: "Raised for a human.", tone: "neutral" },
};

export function handoffReason(reason: string) {
  return (
    handoffReasonMeta[reason] ?? {
      label: reason,
      blurb: "Raised for a human.",
      tone: "neutral" as const,
    }
  );
}

/** The four scored dimensions, in the order a person reads them. */
export const qualificationDimensions = [
  { key: "budget", label: "Budget" },
  { key: "timeline", label: "Timeline" },
  { key: "fit", label: "Fit" },
  { key: "authority", label: "Authority" },
] as const;

export const DIMENSION_MAX = 25;

export function initials(name: string | null | undefined): string {
  if (!name) return "?";
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "?";
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return `${parts[0][0]}${parts[parts.length - 1][0]}`.toUpperCase();
}

/** A lead with no name still needs something recognisable in a list. */
export function displayName(name: string | null | undefined, fallback = "Unknown lead"): string {
  return name && name.trim() ? name.trim() : fallback;
}
