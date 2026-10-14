import { cleanup, render, screen } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import { FollowUpsView } from "@/components/follow-ups/follow-ups-view";
import { Providers } from "@/components/providers";

const LEAD_ID = "7c2504e0-4f89-41d3-9a0c-0305e82c3301";

function task(overrides: Record<string, unknown> = {}) {
  return {
    id: "8ba7b810-9dad-41d1-80b4-00c04fd430c8",
    conversation_id: "6ba7b810-9dad-41d1-80b4-00c04fd430c8",
    lead_id: LEAD_ID,
    task_type: "FOLLOW_UP",
    sequence_step: 1,
    status: "PENDING",
    scheduled_at: new Date(Date.now() + 3600_000).toISOString(),
    attempt_count: 0,
    max_attempts: 3,
    last_error: null,
    ai_enhanced: false,
    sent_text: null,
    completed_at: null,
    cancelled_at: null,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    ...overrides,
  };
}

function stubApi(items: unknown[] | "error" = []) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/v1/leads")) {
        return new Response(
          JSON.stringify({
            // A complete lead, not a two-field stub: the client validates the whole shape,
            // and a thin stub is rejected as an unexpected response - which is the schema
            // working, not a test bug.
            items: [
              {
                id: LEAD_ID,
                tenant_id: "1c2504e0-4f89-41d3-9a0c-0305e82c3301",
                meta_leadgen_id: "lg.abc",
                source: "META_LEAD_ADS",
                status: "QUALIFIED",
                name: "Rahul Sharma",
                email: "rahul@example.test",
                phone: null,
                form_id: null,
                ad_id: null,
                provider_created_at: null,
                field_data: {},
                qualification: null,
                created_at: "2026-09-29T09:00:00Z",
                updated_at: "2026-09-29T09:00:00Z",
              },
            ],
            count: 1,
            total: 1,
          }),
          { headers: { "Content-Type": "application/json" } },
        );
      }
      if (url.includes("scheduled-tasks")) {
        if (items === "error") return new Response("{}", { status: 500 });
        return new Response(JSON.stringify({ items, count: items.length }), {
          headers: { "Content-Type": "application/json" },
        });
      }
      return new Response("{}", { headers: { "Content-Type": "application/json" } });
    }),
  );
}

function renderFollowUps() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <Providers client={client}>
      <FollowUpsView />
    </Providers>,
  );
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("FollowUpsView", () => {
  it("groups by urgency rather than by database status", async () => {
    // The clock is pinned to a mid-afternoon instant, and the reason is that this test
    // previously failed once a day.
    //
    // `groupTasks` buckets a task as "today" when its due time falls on the *calendar* day
    // of `new Date()`, so a fixture of "now + 30 minutes" is only inside today while the
    // suite runs before 23:30. Run at 23:41 and the task legitimately belongs to tomorrow,
    // and the test failed for a reason that had nothing to do with grouping.
    //
    // Pinned rather than made "safe": a fixture that is only sometimes correct hides the
    // failure it is meant to catch. `shouldAdvanceTime` keeps the fake clock running so the
    // async render above still resolves.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(new Date("2026-09-28T14:00:00"));
    try {
      stubApi([
        task({ id: "aaaaaaaa-1111-4111-8111-111111111111", scheduled_at: new Date(Date.now() - 7200_000).toISOString() }),
        task({ id: "aaaaaaaa-2222-4222-8222-222222222222", scheduled_at: new Date(Date.now() + 1800_000).toISOString() }),
        task({
          id: "aaaaaaaa-3333-4333-8333-333333333333",
          status: "CANCELLED",
          last_error: "human_handoff",
          scheduled_at: new Date(Date.now() - 600_000).toISOString(),
        }),
      ]);
      renderFollowUps();

      expect(await screen.findByText("Overdue")).toBeDefined();
      expect(screen.getByText("Due today")).toBeDefined();
      expect(screen.getByText("Stopped")).toBeDefined();
      expect(screen.queryByText("Upcoming")).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it("names the step in product words, not task vocabulary", async () => {
    stubApi([task({ sequence_step: 2 })]);
    renderFollowUps();

    expect(await screen.findByText("Follow-up #2")).toBeDefined();
    expect(document.body.textContent ?? "").not.toMatch(/task_type|sequence_step|locked_by/);
  });

  it("explains a stopped follow-up in the user's terms", async () => {
    stubApi([
      task({
        id: "aaaaaaaa-3333-4333-8333-333333333333",
        status: "CANCELLED",
        last_error: "human_handoff",
      }),
    ]);
    renderFollowUps();

    // "human_handoff" is a database value; the row says what actually happened.
    expect(await screen.findByText(/a person took over the conversation/)).toBeDefined();
  });

  it("turns a ghost task into a lead's state, not a scheduler phase", async () => {
    stubApi([task({ task_type: "GHOST" })]);
    renderFollowUps();

    expect(await screen.findByText("Lead has gone quiet")).toBeDefined();
  });

  it("resolves a lead id to a name the operator recognises", async () => {
    stubApi([task()]);
    renderFollowUps();

    expect(await screen.findByText("Rahul Sharma")).toBeDefined();
  });

  it("links a follow-up to the conversation it belongs to", async () => {
    stubApi([task()]);
    renderFollowUps();

    await screen.findByText("Rahul Sharma");
    const link = screen.getByRole("link", { name: "Open" });
    expect(link.getAttribute("href")).toBe(
      "/conversations/6ba7b810-9dad-41d1-80b4-00c04fd430c8",
    );
  });

  it("explains an empty schedule rather than showing an empty list", async () => {
    stubApi([]);
    renderFollowUps();

    expect(await screen.findByText("No follow-ups scheduled")).toBeDefined();
    expect(screen.getByRole("link", { name: /View conversations/ })).toBeDefined();
  });

  it("separates what already went out", async () => {
    stubApi([
      task({
        id: "aaaaaaaa-4444-4444-8444-444444444444",
        status: "COMPLETED",
        scheduled_at: new Date(Date.now() - 86_400_000).toISOString(),
      }),
    ]);
    renderFollowUps();

    expect(await screen.findByText("Sent")).toBeDefined();
  });

  it("explains a failed load", async () => {
    stubApi("error");
    renderFollowUps();

    expect(await screen.findByText("Could not load follow-ups")).toBeDefined();
  });
});
