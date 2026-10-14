import { cn } from "@/lib/utils";

/**
 * Loading placeholders that match the layout they replace.
 *
 * A full-page spinner tells the user nothing except that something is happening, and it
 * makes the page jump when content replaces it. A skeleton with the *same* shape as the
 * finished screen means the layout never moves, and the user can already see where
 * everything will be.
 *
 * The shimmer respects `prefers-reduced-motion` - the animation is decorative, so a user who
 * has asked for less movement gets a static placeholder rather than none.
 */
export function Skeleton({ className }: { className?: string }) {
  return (
    <div
      aria-hidden
      className={cn(
        "animate-pulse rounded-md bg-muted",
        "motion-reduce:animate-none",
        className,
      )}
    />
  );
}

/** Screen-reader-only text so a loading region is announced, not just drawn. */
export function LoadingRegion({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div role="status" aria-live="polite" aria-busy="true">
      <span className="sr-only">{label}</span>
      {children}
    </div>
  );
}

export function PageSkeleton({ rows = 3 }: { rows?: number }) {
  return (
    <LoadingRegion label="Loading">
      <div className="flex flex-col gap-3">
        {Array.from({ length: rows }, (_, i) => (
          <Skeleton key={i} className="h-16 w-full" />
        ))}
      </div>
    </LoadingRegion>
  );
}

export function StatStripSkeleton({ count = 4 }: { count?: number }) {
  return (
    <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
      {Array.from({ length: count }, (_, i) => (
        <Skeleton key={i} className="h-24 w-full rounded-lg" />
      ))}
    </div>
  );
}
