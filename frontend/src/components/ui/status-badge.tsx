import { cn } from "@/lib/utils";

/**
 * Semantic status pill.
 *
 * Colour is the *only* thing this component is for, and it is deliberately restrained: the
 * product's semantic system is blue for working, green for progress, amber for waiting on a
 * person, red for needing attention, and neutral for everything else. An unworked lead is
 * not a problem, so it must never look like one.
 *
 * Every tone also changes the border and text weight, never colour alone. Roughly one man in
 * twelve cannot reliably separate a red pill from a green one, and "needs a person" is
 * exactly the distinction that must not be lost - so the label always carries the meaning
 * and colour only reinforces it.
 */
export type Tone = "neutral" | "info" | "success" | "warning" | "danger";

const TONES: Record<Tone, string> = {
  neutral: "border-border bg-muted text-muted-foreground",
  info: "border-sky-200 bg-sky-50 text-sky-800 dark:border-sky-900 dark:bg-sky-950 dark:text-sky-200",
  success:
    "border-emerald-200 bg-emerald-50 text-emerald-800 dark:border-emerald-900 dark:bg-emerald-950 dark:text-emerald-200",
  warning:
    "border-amber-300 bg-amber-50 text-amber-900 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-200",
  danger: "border-red-300 bg-red-50 text-red-800 dark:border-red-900 dark:bg-red-950 dark:text-red-200",
};

const DOTS: Record<Tone, string> = {
  neutral: "bg-muted-foreground/60",
  info: "bg-sky-500",
  success: "bg-emerald-500",
  warning: "bg-amber-500",
  danger: "bg-red-500",
};

export function StatusBadge({
  tone = "neutral",
  children,
  dot = false,
  className,
  title,
}: {
  tone?: Tone;
  children: React.ReactNode;
  /** A leading dot for list rows, where the pill text would be noise. */
  dot?: boolean;
  className?: string;
  title?: string;
}) {
  return (
    <span
      title={title}
      className={cn(
        "inline-flex shrink-0 items-center gap-1.5 rounded-full border px-2 py-0.5",
        "text-[11px] leading-4 font-medium whitespace-nowrap",
        TONES[tone],
        className,
      )}
    >
      {dot && <span aria-hidden className={cn("size-1.5 rounded-full", DOTS[tone])} />}
      {children}
    </span>
  );
}

/** A bare coloured dot for dense rows, where a full pill is too much furniture. */
export function StatusDot({ tone = "neutral", label }: { tone?: Tone; label: string }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
      <span aria-hidden className={cn("size-2 rounded-full", DOTS[tone])} />
      {label}
    </span>
  );
}
