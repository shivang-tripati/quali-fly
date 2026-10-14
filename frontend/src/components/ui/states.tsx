import type { LucideIcon } from "lucide-react";

import { Button } from "@/components/ui/button";

/**
 * Empty and error states that explain themselves.
 *
 * Three rules, and they are what separates a useful empty state from "No data.":
 *
 *   1. **What is this screen?** If someone lands here from a nav item with no context, the
 *      heading has to say what the screen is for.
 *   2. **Why is it empty?** "Nothing to show" is not an answer. A handoff queue is empty
 *      because the AI has not escalated anything, which is *good news* and should read like
 *      good news.
 *   3. **What now?** Every state offers a next action, because a dead end is a page the
 *      user learns to avoid.
 *
 * Errors keep the same shape. An error that says "HTTP 409 HANDOFF_INVALID_STATE" makes the
 * user guess; one that says "This handoff was already resolved" tells them what happened and
 * what to do.
 */
export interface StateAction {
  label: string;
  /** Either a route or a handler - not both. `href` renders a link, so it can be opened in a new tab. */
  href?: string;
  onClick?: () => void;
}

export function EmptyState({
  icon: Icon,
  title,
  body,
  action,
  secondaryAction,
  tone = "neutral",
}: {
  icon?: LucideIcon;
  title: string;
  body: string;
  action?: StateAction;
  secondaryAction?: StateAction;
  tone?: "neutral" | "success";
}) {
  return (
    <div className="flex flex-col items-center gap-3 rounded-lg border border-dashed px-6 py-12 text-center">
      {Icon && (
        <span
          aria-hidden
          className={
            tone === "success"
              ? "flex size-10 items-center justify-center rounded-full bg-emerald-50 text-emerald-600 dark:bg-emerald-950 dark:text-emerald-400"
              : "flex size-10 items-center justify-center rounded-full bg-muted text-muted-foreground"
          }
        >
          <Icon className="size-5" />
        </span>
      )}
      <div className="flex flex-col gap-1">
        <p className="text-sm font-medium">{title}</p>
        <p className="max-w-md text-sm text-muted-foreground">{body}</p>
      </div>
      {(action || secondaryAction) && (
        <div className="mt-1 flex flex-wrap items-center justify-center gap-2">
          {action && <StateButton action={action} />}
          {secondaryAction && <StateButton action={secondaryAction} variant="outline" />}
        </div>
      )}
    </div>
  );
}

function StateButton({
  action,
  variant = "default",
}: {
  action: StateAction;
  variant?: "default" | "outline";
}) {
  if (action.href) {
    return (
      <Button asChild size="sm" variant={variant}>
        <a href={action.href}>{action.label}</a>
      </Button>
    );
  }
  return (
    <Button size="sm" variant={variant} onClick={action.onClick}>
      {action.label}
    </Button>
  );
}

export function ErrorState({
  title = "Something went wrong",
  body,
  onRetry,
}: {
  title?: string;
  body: string;
  onRetry?: () => void;
}) {
  return (
    <div
      role="alert"
      className="flex flex-col items-center gap-3 rounded-lg border border-red-200 bg-red-50/50 px-6 py-10 text-center dark:border-red-900 dark:bg-red-950/30"
    >
      <p className="text-sm font-medium text-red-900 dark:text-red-200">{title}</p>
      <p className="max-w-md text-sm text-red-800/80 dark:text-red-200/80">{body}</p>
      {onRetry && (
        <Button size="sm" variant="outline" onClick={onRetry}>
          Try again
        </Button>
      )}
    </div>
  );
}

/**
 * An inline error for a failed mutation.
 *
 * Inline rather than a toast, deliberately: the user just clicked something in front of them
 * and the consequence is in front of them. A toast that disappears takes the explanation
 * with it, and a modal for a failed claim is heavier than the event deserves.
 */
export function InlineError({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const message = error instanceof Error ? error.message : "That did not work.";
  return (
    <p role="alert" className="flex items-center gap-2 text-xs text-destructive">
      <span>{message}</span>
      {onRetry && (
        <button
          type="button"
          onClick={onRetry}
          className="underline underline-offset-2 hover:no-underline"
        >
          Retry
        </button>
      )}
    </p>
  );
}
