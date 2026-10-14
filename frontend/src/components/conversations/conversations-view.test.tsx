import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ConversationsView } from "@/components/conversations/conversations-view";
import { Providers } from "@/components/providers";

const CONVERSATION = {
  id: "6ba7b810-9dad-41d1-80b4-00c04fd430c8",
  channel: "MESSENGER" as const,
  meta_participant_id: "10083363554430212",
  meta_source_id: null,
  meta_referral_ad_id: null,
  lead_id: "7c2504e0-4f89-41d3-9a0c-0305e82c3301",
  lead_name: "Rahul Verma",
  meta_connection_id: "3f2504e0-4f89-41d3-9a0c-0305e82c3301",
  last_message_at: "2026-09-29T09:00:00Z",
  messaging_window_expires_at: "2026-09-30T09:00:00Z",
  within_messaging_window: true,
  created_at: "2026-09-28T08:00:00Z",
  updated_at: "2026-09-29T09:00:00Z",
};

const CLOSED_CONVERSATION = {
  ...CONVERSATION,
  id: "2ba7b810-9dad-41d1-80b4-00c04fd430c9",
  within_messaging_window: false,
  messaging_window_expires_at: "2026-09-28T09:00:00Z",
};

const MESSAGE = {
  id: "11111111-1111-4111-8111-111111111111",
  conversation_id: CONVERSATION.id,
  channel: "MESSENGER" as const,
  direction: "INBOUND" as const,
  delivery: "SENT" as const,
  meta_message_id: "mid.abc",
  message_type: "text",
  text: "Is the car still available?",
  attachments: [],
  sent_at: "2026-09-29T08:55:00Z",
  delivered_at: null,
  failure_reason: null,
  ai_model: null,
  ai_prompt_tokens: null,
  ai_completion_tokens: null,
  ai_latency_ms: null,
  created_at: "2026-09-29T08:55:00Z",
  updated_at: "2026-09-29T08:55:00Z",
};

interface Route {
  conversations?: unknown[];
  messages?: unknown[];
  sendStatus?: number;
}

function stubApi(route: Route = {}) {
  const posts: { url: string; body?: string }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      if (method === "POST") {
        posts.push({ url, body: init?.body as string | undefined });
        return new Response(JSON.stringify(MESSAGE), {
          status: route.sendStatus ?? 201,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.includes("/messages")) {
        return new Response(
          JSON.stringify({ items: route.messages ?? [MESSAGE], count: route.messages?.length ?? 1 }),
          { headers: { "Content-Type": "application/json" } },
        );
      }
      if (url.includes("/handoffs")) {
        return new Response(JSON.stringify({ items: [], count: 0 }), {
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.includes("/conversations")) {
        return new Response(
          JSON.stringify({ items: route.conversations ?? [CONVERSATION], count: 1 }),
          { headers: { "Content-Type": "application/json" } },
        );
      }
      return new Response("{}", { headers: { "Content-Type": "application/json" } });
    }),
  );
  return posts;
}

function renderInbox(selectedId?: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <Providers client={client}>
      <ConversationsView selectedId={selectedId} />
    </Providers>,
  );
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("ConversationsView", () => {
  it("renders a thread with its messages and who wrote them", async () => {
    stubApi();
    renderInbox();

    expect(await screen.findByText("Is the car still available?")).toBeDefined();
    // "Lead" for inbound, not "INBOUND" - the user's word, not the column's.
    expect(screen.getByText("Lead")).toBeDefined();
  });

  it("attributes an AI reply to the AI, and a human reply to you", async () => {
    stubApi({
      messages: [
        MESSAGE,
        { ...MESSAGE, id: "22222222-2222-4222-8222-222222222222", direction: "OUTBOUND", ai_model: "llama-3.1-8b-instant" },
        { ...MESSAGE, id: "33333333-3333-4333-8333-333333333333", direction: "OUTBOUND", ai_model: null },
      ],
    });
    renderInbox();

    expect(await screen.findByText("AI")).toBeDefined();
    expect(screen.getByText("You")).toBeDefined();
  });

  it("disables the composer and says why when Meta's window is closed", async () => {
    stubApi({ conversations: [CLOSED_CONVERSATION], messages: [] });
    renderInbox(CLOSED_CONVERSATION.id);

    const composer = (await screen.findByLabelText(
      "Replies are disabled outside the 24-hour window",
    )) as HTMLTextAreaElement;
    expect(composer.disabled).toBe(true);
    // The reason, not just a dead input box.
    expect(screen.getByText(/24-hour window has closed/)).toBeDefined();
  });

  it("sends a reply through the existing endpoint", async () => {
    const posts = stubApi();
    renderInbox();

    const composer = await screen.findByLabelText("Reply");
    const { fireEvent } = await import("@testing-library/react");
    fireEvent.change(composer, { target: { value: "Yes, still available." } });
    fireEvent.click(screen.getByRole("button", { name: /Send/ }));

    await waitFor(() => expect(posts.some((p) => p.url.includes("/messages"))).toBe(true));
  });

  it("shows a handoff on the thread, because the AI has stood down", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/handoffs")) {
          return new Response(
            JSON.stringify({
              items: [
                {
                  id: "5f7b8100-9dad-41d1-80b4-00c04fd430c8",
                  conversation_id: CONVERSATION.id,
                  lead_id: null,
                  status: "OPEN",
                  reason: "EXPLICIT_HUMAN_REQUEST",
                  detail: null,
                  priority: 300,
                  assigned_user_id: null,
                  assigned_at: null,
                  acknowledged_at: null,
                  acknowledgement_failure: null,
                  acknowledgement_source: null,
                  resolved_at: null,
                  resolution_note: null,
                  lead_name: "Rahul",
                  latest_inbound_text: "call me",
                  latest_inbound_at: null,
                  qualification_total: null,
                  created_at: "2026-09-29T09:00:00Z",
                  updated_at: "2026-09-29T09:00:00Z",
                },
              ],
              count: 1,
            }),
            { headers: { "Content-Type": "application/json" } },
          );
        }
        if (url.includes("/messages")) {
          return new Response(JSON.stringify({ items: [MESSAGE], count: 1 }), {
            headers: { "Content-Type": "application/json" },
          });
        }
        if (url.includes("/conversations")) {
          return new Response(JSON.stringify({ items: [CONVERSATION], count: 1 }), {
            headers: { "Content-Type": "application/json" },
          });
        }
        return new Response("{}", { headers: { "Content-Type": "application/json" } });
      }),
    );
    renderInbox();

    expect(await screen.findByText("Person")).toBeDefined();
    expect(screen.getByRole("link", { name: "Needs a person" })).toBeDefined();
  });

  it("explains an empty inbox and points at the thing that fills it", async () => {
    stubApi({ conversations: [] });
    renderInbox();

    expect(await screen.findByText("No conversations yet")).toBeDefined();
    expect(screen.getByRole("link", { name: /Connect Meta/ })).toBeDefined();
  });

  it("shows the lead's name, not the id Meta knows them by", async () => {
    stubApi();
    renderInbox();

    // In the list and in the thread header. The point is that the name is what is on
    // screen at all - the raw participant id is only ever a secondary line.
    expect((await screen.findAllByText("Rahul Verma")).length).toBeGreaterThan(0);
  });

  it("opens the thread a deep link names, even when it is not in the first page", async () => {
    // The regression this guards: a link to an older thread is not in the list, and
    // selecting from the list alone showed the *newest* thread while the URL said otherwise.
    const older = {
      ...CONVERSATION,
      id: "8ba7b810-9dad-41d1-80b4-00c04fd430ca",
      lead_name: "Priya Nair",
      last_message_at: "2026-08-01T09:00:00Z",
    };
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith(`/conversations/${older.id}`)) {
        return new Response(JSON.stringify(older), {
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.includes("/messages")) {
        return new Response(JSON.stringify({ items: [], count: 0 }), {
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.includes("/handoffs")) {
        return new Response(JSON.stringify({ items: [], count: 0 }), {
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.includes("/conversations")) {
        return new Response(JSON.stringify({ items: [CONVERSATION], count: 1 }), {
          headers: { "Content-Type": "application/json" },
        });
      }
      return new Response("{}", { headers: { "Content-Type": "application/json" } });
    });
    vi.stubGlobal("fetch", fetchMock);
    renderInbox(older.id);

    expect((await screen.findAllByText("Priya Nair")).length).toBeGreaterThan(0);
    // Fetched by id, not merely substituted from the list.
    expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith(older.id))).toBe(true);
  });

  it("says a deep link to a thread this tenant cannot see is unavailable", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        const isList = url.endsWith("/conversations") || url.includes("/conversations?");
        if (url.includes("/conversations") && !isList) return new Response("{}", { status: 404 });
        if (isList) {
          return new Response(JSON.stringify({ items: [CONVERSATION], count: 1 }), {
            headers: { "Content-Type": "application/json" },
          });
        }
        return new Response("{}", { headers: { "Content-Type": "application/json" } });
      }),
    );
    renderInbox("9ba7b810-9dad-41d1-80b4-00c04fd430cb");

    // Not the newest thread, and not a crash: a clear way back.
    expect(await screen.findByText("That conversation is not available")).toBeDefined();
    expect(screen.getByRole("link", { name: /Back to the inbox/ })).toBeDefined();
  });

  it("explains a failed load rather than showing an empty inbox", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 500 })));
    renderInbox();

    expect(await screen.findByText("Could not load conversations")).toBeDefined();
  });

  it("says the thread could not load, instead of showing an empty conversation", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/messages")) return new Response("{}", { status: 500 });
        if (url.includes("/conversations")) {
          return new Response(JSON.stringify({ items: [CONVERSATION], count: 1 }), {
            headers: { "Content-Type": "application/json" },
          });
        }
        return new Response("{}", { headers: { "Content-Type": "application/json" } });
      }),
    );
    renderInbox();

    expect(await screen.findByText(/Could not load this conversation/)).toBeDefined();
  });
});
