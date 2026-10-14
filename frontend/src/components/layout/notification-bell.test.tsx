import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import { NotificationBell } from "@/components/layout/notification-bell";
import { Providers } from "@/components/providers";

const push = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push, replace: vi.fn() }),
  usePathname: () => "/dashboard",
  useSearchParams: () => new URLSearchParams(),
}));

const CONVERSATION_ID = "6ba7b810-9dad-41d1-80b4-00c04fd430c8";

const UNREAD = {
  id: "11111111-1111-4111-8111-111111111111",
  handoff_id: "22222222-2222-4222-8222-222222222222",
  conversation_id: CONVERSATION_ID,
  lead_id: null,
  type: "HANDOFF_CREATED",
  title: "Rahul asked for a person",
  message: "call me",
  payload: {},
  status: "UNREAD",
  read_at: null,
  created_at: "2026-09-29T09:00:00Z",
};

const READ = {
  ...UNREAD,
  id: "33333333-3333-4333-8333-333333333333",
  title: "Older, already read",
  status: "READ",
  read_at: "2026-09-29T09:30:00Z",
};

function stubApi(items: unknown[]) {
  const calls: { url: string; method: string }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      calls.push({ url, method });
      if (url.includes("/notifications/") && url.endsWith("/read")) {
        return new Response(null, { status: 204 });
      }
      if (url.includes("/notifications/inbox")) {
        return new Response(JSON.stringify({ items, count: items.length }), {
          headers: { "Content-Type": "application/json" },
        });
      }
      return new Response("{}", { headers: { "Content-Type": "application/json" } });
    }),
  );
  return calls;
}

function renderBell() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <Providers client={client}>
      <NotificationBell />
    </Providers>,
  );
}

async function openBell() {
  fireEvent.click(await screen.findByRole("button", { name: /Notifications/ }));
  return screen.findByRole("dialog", { name: "Notifications" });
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  push.mockClear();
});

describe("NotificationBell", () => {
  it("badges the unread count and names it for a screen reader", async () => {
    stubApi([UNREAD, READ]);
    renderBell();

    const trigger = await screen.findByRole("button", { name: /Notifications, 1 unread/ });
    expect(trigger).toBeDefined();
    expect(screen.getByText("1")).toBeDefined();
  });

  it("shows no badge when everything has been read", async () => {
    stubApi([READ]);
    renderBell();

    const trigger = await screen.findByRole("button", { name: /none unread/ });
    expect(trigger).toBeDefined();
  });

  it("marks a notification read when it is opened, so the badge can go down", async () => {
    const calls = stubApi([UNREAD]);
    renderBell();
    await openBell();

    fireEvent.click(await screen.findByText("Rahul asked for a person"));

    await waitFor(() =>
      expect(
        calls.some(
          (c) => c.method === "POST" && c.url.endsWith(`/${UNREAD.id}/read`),
        ),
      ).toBe(true),
    );
    expect(push).toHaveBeenCalledWith(`/conversations/${CONVERSATION_ID}`);
  });

  it("does not re-mark something already read", async () => {
    const calls = stubApi([READ]);
    renderBell();
    await openBell();

    fireEvent.click(await screen.findByText("Older, already read"));

    await waitFor(() => expect(push).toHaveBeenCalled());
    expect(calls.some((c) => c.method === "POST")).toBe(false);
  });

  it("sends a failed acknowledgement to the handoff it belongs to", async () => {
    const ackFailed = {
      ...UNREAD,
      type: "HANDOFF_ACK_FAILED",
      title: "Could not tell Rahul we are passing you over",
    };
    stubApi([ackFailed]);
    renderBell();
    await openBell();

    fireEvent.click(await screen.findByText("Could not tell Rahul we are passing you over"));

    await waitFor(() =>
      expect(push).toHaveBeenCalledWith(`/handoffs?focus=${ackFailed.handoff_id}`),
    );
  });
});
