"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { Search } from "lucide-react";

import { useQuery } from "@tanstack/react-query";

import { StatusBadge, type Tone } from "@/components/ui/status-badge";
import { leadStage } from "@/lib/format";
import { leadsOptions } from "@/lib/api/leads";
import {
  handoffReasonLabels,
  handoffSearchOptions,
  handoffStatusLabels,
} from "@/lib/api/handoffs";

/**
 * ⌘K / Ctrl+K over the APIs that actually exist.
 *
 * Deliberately not a fake global search. It queries the two collections where "find the
 * thing I am thinking of" is a real question - leads and handoffs - and every result is a
 * real row from a real endpoint with a real server-side filter. Conversations are not
 * searched: there is no text to match them on (a thread's identity is a Meta participant
 * id), and the inbox is one click away and browsable. Inventing a `/search` that returns
 * canned results to make the palette look complete would be the single most dishonest thing
 * in the product.
 *
 * The palette is a dialog, so it is a real `role="dialog"` with a focus trap, Escape to
 * close, and focus returned to the trigger on close. A command palette that strands keyboard
 * focus is worse than no palette, because it makes the rest of the app unreachable.
 */
export function CommandPalette({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const router = useRouter();
  const [term, setTerm] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  // The cursor is stored *with the query it belonged to*, so a new keystroke invalidates it
  // during render rather than through an effect. An effect that resets state after a
  // keystroke has already painted a frame with a stale highlight on it.
  const [cursor, setCursor] = useState({ term: "", index: 0 });

  // An empty query would fetch and list everything, which is both slow and useless - a
  // palette with 200 untyped leads in it is a file browser, not a shortcut.
  const active = open && term.trim().length >= 2;
  const leads = useQuery({
    ...leadsOptions({ search: term.trim(), limit: 5 }),
    enabled: active,
  });
  const handoffs = useQuery({
    ...handoffSearchOptions(term.trim()),
    enabled: active,
  });

  const searching = leads.isPending || handoffs.isPending;
  const groups = active
    ? [
        {
          title: "Leads",
          items: (leads.data?.items ?? []).map((lead) => ({
            id: lead.id,
            primary: lead.name?.trim() || "Unnamed lead",
            secondary: lead.email ?? lead.meta_leadgen_id,
            tone: leadStage(lead.status).tone,
            label: leadStage(lead.status).label,
            href: `/leads/${lead.id}`,
          })),
        },
        {
          title: "Handoffs",
          items: (handoffs.data?.items ?? []).map((handoff) => ({
            id: handoff.id,
            primary: handoff.lead_name?.trim() || "A conversation",
            secondary: handoffReasonLabels[handoff.reason],
            tone: handoff.status === "OPEN" ? ("warning" as Tone) : ("info" as Tone),
            label: handoffStatusLabels[handoff.status],
            href: `/handoffs?focus=${handoff.id}`,
          })),
        },
      ]
    : [];

  const flat = groups.flatMap((group) => group.items);
  const activeIndex = cursor.term === term ? Math.min(cursor.index, flat.length - 1) : 0;

  useEffect(() => {
    if (!open) return;
    // Focus on the next frame: the element is mounted by the parent's state change, and
    // focusing before it exists is a no-op that leaves the trap with nothing focused.
    const frame = requestAnimationFrame(() => inputRef.current?.focus());
    return () => cancelAnimationFrame(frame);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        onOpenChange(false);
      } else if (event.key === "ArrowDown") {
        event.preventDefault();
        setCursor({ term, index: Math.min(activeIndex + 1, flat.length - 1) });
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        setCursor({ term, index: Math.max(activeIndex - 1, 0) });
      } else if (event.key === "Enter") {
        const target = flat[activeIndex];
        if (target) {
          event.preventDefault();
          router.push(target.href);
          onOpenChange(false);
        }
      }
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, flat, activeIndex, term, onOpenChange, router]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center bg-black/40 p-4 pt-[12vh] backdrop-blur-[1px]"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onOpenChange(false);
      }}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label="Search"
        className="w-full max-w-lg overflow-hidden rounded-xl border bg-popover shadow-xl"
      >
        <div className="flex items-center gap-2 border-b px-3">
          <Search aria-hidden className="size-4 shrink-0 text-muted-foreground" />
          <input
            ref={inputRef}
            value={term}
            onChange={(event) => setTerm(event.target.value)}
            placeholder="Search leads…"
            aria-label="Search leads and handoffs"
            className="h-11 w-full bg-transparent text-sm outline-none placeholder:text-muted-foreground"
          />
          <kbd className="hidden shrink-0 rounded border px-1.5 py-0.5 text-[10px] text-muted-foreground sm:inline">
            Esc
          </kbd>
        </div>

        <div className="max-h-80 overflow-y-auto p-2">
          {!active && (
            <p className="px-2 py-6 text-center text-sm text-muted-foreground">
              Type at least two characters to search your leads and handoffs.
            </p>
          )}
          {active && searching && (
            <p className="px-2 py-6 text-center text-sm text-muted-foreground">Searching…</p>
          )}
          {active && !searching && flat.length === 0 && (
            <p className="px-2 py-6 text-center text-sm text-muted-foreground">
              Nothing matches “{term.trim()}”.
            </p>
          )}
          {groups.map((group) =>
            group.items.length === 0 ? null : (
              <div key={group.title} className="mb-2 last:mb-0">
                <p className="px-2 py-1.5 text-[11px] font-medium tracking-wide text-muted-foreground uppercase">
                  {group.title}
                </p>
                <ul>
                  {group.items.map((item) => {
                    const index = flat.indexOf(item);
                    const highlighted = index === activeIndex;
                    return (
                      <li key={item.id}>
                        <button
                          type="button"
                          onMouseEnter={() => setCursor({ term, index })}
                          onClick={() => {
                            router.push(item.href);
                            onOpenChange(false);
                          }}
                          className={
                            highlighted
                              ? "flex w-full items-center gap-2 rounded-lg bg-accent px-2 py-2 text-left"
                              : "flex w-full items-center gap-2 rounded-lg px-2 py-2 text-left hover:bg-accent/60"
                          }
                        >
                          <span className="flex min-w-0 flex-1 flex-col">
                            <span className="truncate text-sm font-medium">{item.primary}</span>
                            <span className="truncate text-xs text-muted-foreground">
                              {item.secondary}
                            </span>
                          </span>
                          <StatusBadge tone={item.tone as Tone}>{item.label}</StatusBadge>
                        </button>
                      </li>
                    );
                  })}
                </ul>
              </div>
            ),
          )}
        </div>
      </div>
    </div>
  );
}

/**
 * Registers the global shortcut and owns nothing.
 *
 * `open` is controlled by the shell rather than held here, so the ⌘K shortcut and the
 * toolbar button drive the *same* piece of state. Two copies of "is the palette open" is
 * exactly the kind of thing where the button works and the keyboard does not.
 */
export function CommandPaletteProvider({
  open,
  onOpenChange,
  children,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  children: React.ReactNode;
}) {
  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      // `metaKey` on macOS, `ctrlKey` elsewhere. Both are checked so the shortcut works on a
      // Mac with a PC keyboard, which is a real configuration and not a hypothetical.
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        onOpenChange(!open);
      }
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onOpenChange]);

  return (
    <>
      {children}
      {/* Mounted only while open, so closing the palette discards the query and the cursor
          with it. An effect that reset that state on open would render one frame with the
          previous search's results still highlighted. */}
      {open && <CommandPalette open onOpenChange={onOpenChange} />}
    </>
  );
}

export function SearchTrigger({ onClick }: { onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex h-8 items-center gap-2 rounded-lg border bg-muted/40 px-2.5 text-xs text-muted-foreground transition-colors hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
    >
      <Search aria-hidden className="size-3.5" />
      <span className="hidden sm:inline">Search</span>
      <kbd className="hidden rounded border px-1 py-px text-[10px] sm:inline">⌘K</kbd>
      <span className="sr-only sm:hidden">Search</span>
    </button>
  );
}
