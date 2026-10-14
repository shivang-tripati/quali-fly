import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DashboardView } from "@/components/dashboard/dashboard-view";
import { Providers } from "@/components/providers";

const BASE = {
  summary: {
    new_leads: 12,
    new_leads_today: 5,
    qualified_leads: 5,
    handoffs_open: 3,
    handoffs_unassigned: 2,
    handoffs_mine: 1,
    follow_ups_due: 7,
    follow_ups_overdue: 2,
    follow_ups_upcoming: 3,
    knowledge_documents: 0,
  },
  lead_activity: { new: 12, qualified: 5, replying: 8, handoff: 3, closed: 1 },
  recent_conversations: [
    {
      id: "6ba7b810-9dad-41d1-80b4-00c04fd430c8",
      lead_id: "7c2504e0-4f89-41d3-9a0c-0305e82c3301",
      lead_name: "Rahul Sharma",
      lead_status: "QUALIFIED",
      channel: "MESSENGER",
      last_message_text: "What is the monthly price?",
      last_message_direction: "INBOUND",
      last_message_delivery: "SENT",
      last_message_at: "2026-09-29T09:00:00Z",
      has_active_handoff: false,
      within_messaging_window: true,
    },
  ],
  integration: {
    meta_connected: true,
    meta_needs_attention: false,
    meta_connection_count: 1,
    last_webhook_at: "2026-09-29T09:00:00Z",
    knowledge_documents: 0,
    knowledge_chunks: 0,
  },
  generated_at: "2026-09-29T09:00:00Z",
};

type Route = { status?: number; body?: unknown };

function stubApi(route: Route = {}) {
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      calls.push(url);
      if (url.includes("/dashboard")) {
        return new Response(JSON.stringify(route.body ?? BASE), {
          status: route.status ?? 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      return new Response("{}", { headers: { "Content-Type": "application/json" } });
    }),
  );
  return calls;
}

function renderDashboard() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <Providers client={client}>
      <DashboardView />
    </Providers>,
  );
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("DashboardView", () => {
  it("leads with what needs attention, not with a chart", async () => {
    stubApi({
      body: {
        ...BASE,
        attention: [
          {
            kind: "handoff",
            severity: "critical",
            title: "Rahul Sharma",
            detail: "Rahul Sharma asked to speak to someone.",
            waiting_seconds: 1080,
            conversation_id: "6ba7b810-9dad-41d1-80b4-00c04fd430c8",
            handoff_id: "5f7b8100-9dad-41d1-80b4-00c04fd430c8",
            lead_id: "7c2504e0-4f89-41d3-9a0c-0305e82c3301",
            label: "UNCLAIMED",
          },
        ],
      },
    });
    renderDashboard();

    // The greeting is context; the queue is the point.
    expect(await screen.findByText(/conversation is waiting on a person/i)).toBeDefined();
    expect(screen.getByText("Needs your attention")).toBeDefined();
    // "Rahul Sharma" is in the attention queue *and* in recent conversations - both matter.
    expect(screen.getAllByText("Rahul Sharma").length).toBeGreaterThan(0);
    // 1080 seconds reads as a human duration, not a raw epoch.
    expect(screen.getByText(/waiting 18 minutes/)).toBeDefined();
  });

  it("links an unclaimed handoff straight to the queue with a Claim action", async () => {
    stubApi({
      body: {
        ...BASE,
        attention: [
          {
            kind: "handoff",
            severity: "critical",
            title: "Rahul Sharma",
            detail: "asked to speak to someone",
            waiting_seconds: 60,
            conversation_id: "6ba7b810-9dad-41d1-80b4-00c04fd430c8",
            handoff_id: "5f7b8100-9dad-41d1-80b4-00c04fd430c8",
            lead_id: null,
            label: "UNCLAIMED",
          },
        ],
      },
    });
    renderDashboard();

    const claim = await screen.findByRole("link", { name: /Claim/ });
    expect(claim.getAttribute("href")).toBe("/handoffs?focus=5f7b8100-9dad-41d1-80b4-00c04fd430c8");
  });

  it("links an overdue follow-up to the conversation, not the scheduler", async () => {
    stubApi({
      body: {
        ...BASE,
        attention: [
          {
            kind: "follow_up_overdue",
            severity: "warning",
            title: "Priya Singh",
            detail: "Follow-up for Priya Singh was not sent.",
            waiting_seconds: 7200,
            conversation_id: "6ba7b810-9dad-41d1-80b4-00c04fd430c8",
            handoff_id: null,
            lead_id: null,
            label: "OVERDUE",
          },
        ],
      },
    });
    renderDashboard();

    const action = await screen.findByRole("link", { name: /Open conversation/ });
    expect(action.getAttribute("href")).toBe(
      "/conversations/6ba7b810-9dad-41d1-80b4-00c04fd430c8",
    );
  });

  it("never renders scheduler internals, because the API does not send them", async () => {
    stubApi({
      body: {
        ...BASE,
        attention: [
          {
            kind: "handoff",
            severity: "critical",
            title: "Rahul Sharma",
            detail: "asked to speak to someone",
            waiting_seconds: 60,
            conversation_id: "6ba7b810-9dad-41d1-80b4-00c04fd430c8",
            handoff_id: "5f7b8100-9dad-41d1-80b4-00c04fd430c8",
            lead_id: null,
            label: "UNCLAIMED",
          },
        ],
      },
    });
    renderDashboard();

    await screen.findByText("Needs your attention");
    expect(document.body.textContent ?? "").not.toMatch(/sequence_step|locked_by|task_type|PENDING/);
  });

  it("says nothing needs attention when the queue is empty", async () => {
    stubApi({ body: { ...BASE, attention: [] } });
    renderDashboard();

    expect(await screen.findByText("Nothing needs you right now")).toBeDefined();
    // A calm empty state, not a red alert.
    expect(screen.queryByText(/UNCLAIMED/)).toBeNull();
  });

  it("shows the summary strip from real numbers", async () => {
    stubApi({ body: { ...BASE, attention: [] } });
    renderDashboard();

    await screen.findByText("New leads");
    // 12 appears in the strip and in the activity bar; both come from the same payload.
    expect(screen.getAllByText("12").length).toBeGreaterThan(0);
    expect(screen.getByText("Handoffs")).toBeDefined();
    expect(screen.getByText(/2 unassigned/)).toBeDefined();
  });

  it("links every stat to a filtered list", async () => {
    stubApi({ body: { ...BASE, attention: [] } });
    renderDashboard();

    await screen.findByText("New leads");
    // "Qualified" is both a stat and an activity stage; take the stat by its link href.
    const qualified = screen
      .getAllByText("Qualified")
      .map((el) => el.closest("a"))
      .find((a) => a?.getAttribute("href") === "/leads?status=QUALIFIED");
    expect(qualified).toBeDefined();
    const handoffs = screen
      .getAllByText("Handoffs")
      .map((el) => el.closest("a"))
      .find((a) => a?.getAttribute("href") === "/handoffs");
    expect(handoffs).toBeDefined();
  });

  it("shows the lead activity bar", async () => {
    stubApi({ body: { ...BASE, attention: [] } });
    renderDashboard();

    await screen.findByText("Lead activity");
    // Each stage is a link into the filtered list, with its count.
    const stage = screen.getAllByText("Needs a person")[0].closest("a");
    expect(stage?.getAttribute("href")).toBe("/leads?status=HUMAN_HANDOFF");
  });

  it("explains an empty pipeline instead of showing a blank chart", async () => {
    stubApi({
      body: {
        ...BASE,
        lead_activity: { new: 0, qualified: 0, replying: 0, handoff: 0, closed: 0 },
        attention: [],
      },
    });
    renderDashboard();

    expect(await screen.findByText("No leads yet")).toBeDefined();
  });

  it("links a recent conversation to its thread", async () => {
    stubApi({ body: { ...BASE, attention: [] } });
    renderDashboard();

    await screen.findByText("Recent conversations");
    const link = screen.getAllByText("Rahul Sharma")[0].closest("a");
    expect(link?.getAttribute("href")).toBe(
      "/conversations/6ba7b810-9dad-41d1-80b4-00c04fd430c8",
    );
  });

  it("hides automation health from a member, because the API omits it", async () => {
    stubApi({ body: { ...BASE, attention: [], integration: null } });
    renderDashboard();

    await screen.findByText("Recent conversations");
    expect(screen.queryByText("Automation health")).toBeNull();
  });

  it("surfaces a broken Meta connection in health and as an attention item", async () => {
    stubApi({
      body: {
        ...BASE,
        integration: {
          meta_connected: false,
          meta_needs_attention: true,
          meta_connection_count: 1,
          last_webhook_at: null,
          knowledge_documents: 0,
          knowledge_chunks: 0,
        },
        attention: [
          {
            kind: "connection",
            severity: "critical",
            title: "Meta connection needs attention",
            detail: "needs to be reconnected",
            waiting_seconds: 0,
            conversation_id: null,
            handoff_id: null,
            lead_id: null,
            label: "RECONNECT",
          },
        ],
      },
    });
    renderDashboard();

    const reconnect = await screen.findByRole("link", { name: /Reconnect/ });
    expect(reconnect.getAttribute("href")).toBe("/settings?tab=meta");
    expect(await screen.findByText("Automation health")).toBeDefined();
  });

  it("explains a failed load instead of rendering an empty dashboard", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 500 })));
    renderDashboard();

    expect(await screen.findByText("Could not load your dashboard")).toBeDefined();
  });

  it("renders a skeleton rather than a spinner while loading", async () => {
    let resolveFetch: (value: Response) => void = () => {};
    vi.stubGlobal(
      "fetch",
      vi.fn(() => new Promise<Response>((resolve) => (resolveFetch = resolve))),
    );
    renderDashboard();

    // The loading state announces itself to a screen reader, not just to the eye.
    expect(screen.getAllByRole("status").length).toBeGreaterThan(0);
    expect(screen.getByText("Loading your dashboard")).toBeDefined();

    resolveFetch(
      new Response(JSON.stringify({ ...BASE, attention: [] }), {
        headers: { "Content-Type": "application/json" },
      }),
    );
    await waitFor(() => expect(screen.getByText("New leads")).toBeDefined());
  });
});
