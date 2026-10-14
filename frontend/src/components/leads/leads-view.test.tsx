import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import { LeadsView } from "@/components/leads/leads-view";
import { Providers } from "@/components/providers";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

const LEAD = {
  id: "7c2504e0-4f89-41d3-9a0c-0305e82c3301",
  tenant_id: "1c2504e0-4f89-41d3-9a0c-0305e82c3301",
  meta_leadgen_id: "lg.abc",
  source: "META_LEAD_ADS" as const,
  status: "QUALIFIED" as const,
  name: "Priya Singh",
  email: "priya@example.test",
  phone: null,
  form_id: null,
  ad_id: null,
  provider_created_at: null,
  field_data: {},
  qualification: { budget: 21, timeline: 18, fit: 23, authority: 19, total: 81, high_intent: true },
  created_at: "2026-09-29T09:00:00Z",
  updated_at: "2026-09-29T09:00:00Z",
};

interface Route {
  items?: unknown[];
  total?: number;
  status?: number;
  body?: unknown;
}

function stubApi(route: Route = {}) {
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      calls.push(url);
      if (url.includes("/api/v1/leads")) {
        // A search that does not match returns nothing, so the empty state is reachable
        // without a second stub shape.
        const searching = url.includes("q=");
        const items = route.items ?? (searching ? [] : [LEAD]);
        return new Response(
          JSON.stringify(
            route.body ?? { items, count: items.length, total: route.total ?? items.length },
          ),
          { status: route.status ?? 200, headers: { "Content-Type": "application/json" } },
        );
      }
      return new Response("{}", { headers: { "Content-Type": "application/json" } });
    }),
  );
  return calls;
}

function renderLeads() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <Providers client={client}>
      <LeadsView />
    </Providers>,
  );
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("LeadsView", () => {
  it("lists a lead with its stage and how to reach them", async () => {
    stubApi();
    renderLeads();

    expect(await screen.findByText("Priya Singh")).toBeDefined();
    // "Qualified" is both a filter pill and the lead's badge; the row is what matters.
    expect(screen.getAllByText("Qualified").length).toBeGreaterThan(0);
    expect(screen.getByText("High intent")).toBeDefined();
    expect(screen.getByText("priya@example.test")).toBeDefined();
  });

  it("links a lead to its own workspace", async () => {
    stubApi();
    renderLeads();

    await screen.findByText("Priya Singh");
    const link = screen.getByText("Priya Singh").closest("a");
    expect(link?.getAttribute("href")).toBe(`/leads/${LEAD.id}`);
  });

  it("sends the search term to the API", async () => {
    const calls = stubApi();
    renderLeads();

    // `fireEvent.change` rather than assigning `.value`: React tracks the previous value on
    // the DOM node, so a bare assignment is swallowed and the input never updates.
    fireEvent.change(await screen.findByLabelText("Search leads"), {
      target: { value: "priya" },
    });

    // Debounced, so the request lands after the timer rather than per keystroke.
    await waitFor(() => expect(calls.some((url) => url.includes("q=priya"))).toBe(true), {
      timeout: 2000,
    });
  });

  it("shows a useful empty state when a search matches nothing", async () => {
    const calls = stubApi();
    renderLeads();

    fireEvent.change(await screen.findByLabelText("Search leads"), {
      target: { value: "nobody" },
    });

    expect(await screen.findByText(/No lead matches/, {}, { timeout: 2000 })).toBeDefined();
    // The API was actually asked, rather than the list just being filtered locally.
    await waitFor(() => expect(calls.some((url) => url.includes("q=nobody"))).toBe(true));
  });

  it("offers a next action when there are no leads at all", async () => {
    stubApi({ items: [], total: 0 });
    renderLeads();

    expect(await screen.findByText("No leads yet")).toBeDefined();
    const connect = screen.getByRole("link", { name: /Connect Meta/ });
    expect(connect.getAttribute("href")).toBe("/settings?tab=meta");
  });

  it("paginates against the server total, counting the rows actually shown", async () => {
    stubApi({ items: [LEAD], total: 60 });
    renderLeads();

    // One row on this page, so the range ends at 1 - not at the page size. Claiming "1-25"
    // when a single lead is on screen is the sort of small lie that erodes trust in a count.
    const nav = await screen.findByRole("navigation", { name: "Pagination" });
    expect(nav.textContent).toMatch(/Showing 1.+1 of 60/);
    expect((screen.getByRole("button", { name: "Next" }) as HTMLButtonElement).disabled).toBe(
      false,
    );
    expect((screen.getByRole("button", { name: "Previous" }) as HTMLButtonElement).disabled).toBe(
      true,
    );
  });

  it("explains a failed load rather than showing an empty list", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 500 })));
    renderLeads();

    expect(await screen.findByText("Could not load your leads")).toBeDefined();
  });

  it("shows a skeleton while loading", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => new Promise<Response>(() => {})),
    );
    renderLeads();

    expect(screen.getByText("Loading leads")).toBeDefined();
  });
});
