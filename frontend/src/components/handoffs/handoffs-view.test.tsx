import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import { HandoffsView } from "@/components/handoffs/handoffs-view";
import { Providers } from "@/components/providers";

vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(),
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
}));

vi.mock("@/lib/auth/hooks", () => ({
  useCurrentUser: () => ({ user: { id: "aaaa1111-1111-4111-8111-111111111111" }, isLoading: false }),
}));

const HANDOFF = {
  id: "5f7b8100-9dad-41d1-80b4-00c04fd430c8",
  conversation_id: "6ba7b810-9dad-41d1-80b4-00c04fd430c8",
  lead_id: "7c2504e0-4f89-41d3-9a0c-0305e82c3301",
  status: "OPEN" as const,
  reason: "EXPLICIT_HUMAN_REQUEST" as const,
  detail: "wants a call",
  priority: 300,
  assigned_user_id: null,
  assigned_at: null,
  acknowledged_at: null,
  acknowledgement_failure: null,
  acknowledgement_source: "TEMPLATE",
  resolved_at: null,
  resolution_note: null,
  lead_name: "Rahul Sharma",
  latest_inbound_text: "Can someone call me about pricing?",
  latest_inbound_at: "2026-09-29T09:00:00Z",
  qualification_total: 40,
  created_at: "2026-09-29T09:00:00Z",
  updated_at: "2026-09-29T09:00:00Z",
};

interface Route {
  items?: unknown[];
  status?: number;
  claimStatus?: number;
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
        // A lost race comes back as the backend's real error envelope, which is what
        // `apiRequest` unwraps into a human message. The stub has to send that shape or
        // the test would be asserting the client's fallback rather than the real copy.
        if ((route.claimStatus ?? 200) >= 400) {
          return new Response(
            JSON.stringify({
              error: {
                code: "HANDOFF_ALREADY_ASSIGNED",
                message: "This handoff was claimed by another user a moment ago.",
              },
            }),
            { status: route.claimStatus, headers: { "Content-Type": "application/json" } },
          );
        }
        return new Response(JSON.stringify({ ...HANDOFF, status: "ASSIGNED" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.includes("/handoffs")) {
        return new Response(
          JSON.stringify({ items: route.items ?? [HANDOFF], count: 1, summary: { OPEN: 1 } }),
          { status: route.status ?? 200, headers: { "Content-Type": "application/json" } },
        );
      }
      return new Response("{}", { headers: { "Content-Type": "application/json" } });
    }),
  );
  return posts;
}

function renderQueue() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <Providers client={client}>
      <HandoffsView />
    </Providers>,
  );
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("HandoffsView", () => {
  it("gives enough context to claim without opening anything else", async () => {
    stubApi();
    renderQueue();

    // Who, why, and what they said - all on the row.
    expect(await screen.findAllByText("Rahul Sharma")).toBeDefined();
    expect(screen.getByText("Asked for a person")).toBeDefined();
    expect(screen.getAllByText(/Can someone call me/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/unclaimed/).length).toBeGreaterThan(0);
  });

  it("claims through the real endpoint", async () => {
    const posts = stubApi();
    renderQueue();

    (await screen.findAllByRole("button", { name: "Claim" }))[0].click();

    await waitFor(() =>
      expect(posts.some((p) => p.url.includes("/claim") && p.url.includes(HANDOFF.id))).toBe(
        true,
      ),
    );
  });

  it("surfaces a failed acknowledgement, because it changes the next step", async () => {
    stubApi({
      items: [{ ...HANDOFF, acknowledged_at: null, acknowledgement_failure: "meta_unavailable" }],
    });
    renderQueue();

    expect(await screen.findByText(/was not delivered/i)).toBeDefined();
    expect(screen.getAllByText(/The lead was not told someone is coming/i).length).toBeGreaterThan(
      0,
    );
  });

  it("hides Claim once someone owns the handoff, and offers Start instead", async () => {
    stubApi({ items: [{ ...HANDOFF, status: "ASSIGNED", assigned_user_id: "aaaa1111-1111-4111-8111-111111111111" }] });
    renderQueue();

    await screen.findAllByText("Rahul Sharma");
    expect(screen.queryByRole("button", { name: "Claim" })).toBeNull();
    expect(screen.getAllByRole("button", { name: "Start" }).length).toBeGreaterThan(0);
  });

  it("shows no actions on a resolved handoff", async () => {
    stubApi({ items: [{ ...HANDOFF, status: "RESOLVED", resolved_at: "2026-09-29T11:00:00Z" }] });
    renderQueue();

    // Switch to "All" so the resolved row is visible at all.
    (await screen.findByRole("button", { name: "All" })).click();

    await waitFor(() => expect(screen.queryByRole("button", { name: "Claim" })).toBeNull());
  });

  it("says the queue is clear, as good news rather than an error", async () => {
    stubApi({ items: [] });
    renderQueue();

    expect(await screen.findByText("No handoffs waiting")).toBeDefined();
    expect(screen.getByRole("link", { name: /View conversations/ })).toBeDefined();
  });

  it("links through to the conversation", async () => {
    stubApi();
    renderQueue();

    await screen.findAllByText("Rahul Sharma");
    const link = screen.getByRole("link", { name: /Open conversation/ });
    expect(link.getAttribute("href")).toBe(`/conversations/${HANDOFF.conversation_id}`);
  });

  it("reports a lost claim race in the user's words, not a status code", async () => {
    stubApi({ claimStatus: 409 });
    renderQueue();

    (await screen.findAllByRole("button", { name: "Claim" }))[0].click();

    // The backend's sentence, not "HTTP 409 HANDOFF_ALREADY_ASSIGNED".
    expect(
      await screen.findByText(/claimed by another user a moment ago/i),
    ).toBeDefined();
    expect(document.body.textContent ?? "").not.toMatch(/HANDOFF_ALREADY_ASSIGNED/);
  });

  it("explains a failed load rather than showing an empty queue", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 500 })));
    renderQueue();

    expect(await screen.findByText("Could not load the handoff queue")).toBeDefined();
  });
});
