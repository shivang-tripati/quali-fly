"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Bell, Check } from "lucide-react";
import { useQuery } from "@tanstack/react-query";

import { handoffNotificationsOptions, useMarkNotificationRead, type HandoffNotification } from "@/lib/api/handoffs";
import { relativeTime } from "@/lib/format";
import { Button } from "@/components/ui/button";

/**
 * The notification bell.
 *
 * Notifications are Phase 8's *internal* records, which means they are already the right
 * shape for this: "a conversation was escalated", "the acknowledgement to the lead failed".
 * No new notification type was invented for the dashboard, because a second notification
 * system is how a product ends up with two bells and no idea which one to trust.
 *
 * Unread count comes from the same payload as the list, so the badge and the dropdown can
 * never disagree. The dropdown is a `role="dialog"` with Escape to close and a click-outside
 * to dismiss; a popover that traps nothing and closes on nothing is worse than a list.
 */
export function NotificationBell() {
  const [open, setOpen] = useState(false);
  const router = useRouter();
  const notifications = useQuery(handoffNotificationsOptions());
  const markRead = useMarkNotificationRead();

  const items = notifications.data?.items ?? [];
  const unread = items.filter((n) => n.status === "UNREAD");

  /**
   * Go to the thing, and record that this person has now seen it.
   *
   * The item is optional because the footer's "open the queue" link is a navigation
   * shortcut, not a notification - there is nothing there to mark read.
   */
  function visit(href: string, item?: HandoffNotification) {
    setOpen(false);
    if (item?.status === "UNREAD") markRead.mutate(item.id);
    router.push(href);
  }

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        aria-haspopup="dialog"
        aria-label={
          unread.length > 0
            ? `Notifications, ${unread.length} unread`
            : "Notifications, none unread"
        }
        className="relative flex size-8 items-center justify-center rounded-lg text-muted-foreground transition-colors hover:bg-accent hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
      >
        <Bell aria-hidden className="size-4" />
        {unread.length > 0 && (
          <span className="absolute -top-0.5 -right-0.5 inline-flex h-4 min-w-4 items-center justify-center rounded-full bg-amber-500 px-1 text-[10px] font-bold text-white">
            {unread.length > 9 ? "9+" : unread.length}
            <span className="sr-only"> unread notifications</span>
          </span>
        )}
      </button>

      {open && (
        <>
          <button
            type="button"
            aria-label="Close notifications"
            className="fixed inset-0 z-40 cursor-default"
            onClick={() => setOpen(false)}
          />
          <div
            role="dialog"
            aria-label="Notifications"
            className="absolute right-0 z-50 mt-2 w-80 overflow-hidden rounded-xl border bg-popover shadow-lg"
          >
            <div className="flex items-center justify-between border-b px-3 py-2">
              <p className="text-sm font-medium">Notifications</p>
              {unread.length > 0 && (
                <span className="text-xs text-muted-foreground">{unread.length} unread</span>
              )}
            </div>
            <div
              className="max-h-96 overflow-y-auto"
              onKeyDown={(event) => {
                if (event.key === "Escape") setOpen(false);
              }}
            >
              {items.length === 0 && (
                <p className="px-3 py-6 text-center text-sm text-muted-foreground">
                  Nothing yet. You will hear about a conversation here when the AI decides it
                  needs a person.
                </p>
              )}
              <ul>
                {items.map((item) => {
                  const href =
                    item.type === "HANDOFF_ACK_FAILED"
                      ? `/handoffs?focus=${item.handoff_id}`
                      : `/conversations/${item.conversation_id}`;
                  return (
                    <li key={item.id} className="border-b last:border-b-0">
                      <button
                        type="button"
                        onClick={() => visit(href, item)}
                        className="flex w-full flex-col gap-1 px-3 py-2.5 text-left transition-colors hover:bg-accent/60 focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
                      >
                        <span className="flex items-center gap-2">
                          <span
                            aria-hidden
                            className={
                              item.status === "UNREAD"
                                ? "size-1.5 shrink-0 rounded-full bg-amber-500"
                                : "size-1.5 shrink-0"
                            }
                          />
                          <span className="truncate text-sm font-medium">{item.title}</span>
                          {item.status === "READ" && (
                            <Check aria-hidden className="ml-auto size-3 text-muted-foreground" />
                          )}
                        </span>
                        <span className="line-clamp-2 pl-3.5 text-xs text-muted-foreground">
                          {item.message}
                        </span>
                        <span className="pl-3.5 text-[11px] text-muted-foreground/80">
                          {relativeTime(item.created_at)}
                        </span>
                      </button>
                    </li>
                  );
                })}
              </ul>
            </div>
            <div className="border-t p-2">
              <Button
                variant="ghost"
                size="sm"
                className="w-full"
                onClick={() => visit("/handoffs")}
              >
                Open the handoff queue
              </Button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}