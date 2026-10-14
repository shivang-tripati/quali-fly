import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import { KnowledgeView } from "@/components/knowledge/knowledge-view";
import { Providers } from "@/components/providers";

const DOCUMENT = {
  document_id: "4c2504e0-4f89-41d3-9a0c-0305e82c3301",
  title: "Pricing FAQ",
  source_type: "faq",
  is_active: true,
  chunk_count: 14,
  updated_at: "2026-09-29T07:00:00Z",
};

interface Route {
  items?: unknown[];
  saveStatus?: number;
}

function stubApi(route: Route = {}) {
  const requests: { url: string; method: string; body?: string }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      requests.push({ url, method, body: init?.body as string | undefined });
      if (method === "POST") {
        return new Response(JSON.stringify(DOCUMENT), {
          status: route.saveStatus ?? 201,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.includes("knowledge-base")) {
        return new Response(
          JSON.stringify({ items: route.items ?? [DOCUMENT], count: route.items?.length ?? 1 }),
          { headers: { "Content-Type": "application/json" } },
        );
      }
      return new Response("{}", { headers: { "Content-Type": "application/json" } });
    }),
  );
  return requests;
}

function renderKnowledge() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <Providers client={client}>
      <KnowledgeView />
    </Providers>,
  );
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("KnowledgeView", () => {
  it("answers 'what does my AI know?' in documents, not rows", async () => {
    stubApi();
    renderKnowledge();

    expect(await screen.findByText("Pricing FAQ")).toBeDefined();
    // Source, size and recency on one line, because that is what "have I added it yet?"
    // actually needs answered.
    expect(screen.getByText(/FAQ · 14 pieces · updated/)).toBeDefined();
  });

  it("links each document to the screen that shows what the AI actually has", async () => {
    // The regression: `/knowledge/[id]` and its view both existed, but nothing in the list
    // pointed at them, so the detail screen was reachable only by typing a URL. The list is
    // the only place a person is looking, so the link has to be here.
    stubApi();
    renderKnowledge();

    const link = await screen.findByRole("link", { name: "Pricing FAQ" });
    expect(link.getAttribute("href")).toBe("/knowledge/4c2504e0-4f89-41d3-9a0c-0305e82c3301");
  });

  it("tells the operator the AI has nothing to answer from, and how to fix it", async () => {
    stubApi({ items: [] });
    renderKnowledge();

    expect(await screen.findByText("Your AI has nothing to answer from yet")).toBeDefined();
    expect(screen.getByRole("button", { name: /Add your first document/ })).toBeDefined();
  });

  it("saves a document through the existing ingest endpoint", async () => {
    const requests = stubApi();
    renderKnowledge();

    (await screen.findByRole("button", { name: /Add knowledge/ })).click();

    fireEvent.change(await screen.findByLabelText("Title"), { target: { value: "Refunds" } });
    fireEvent.change(await screen.findByLabelText("Content"), {
      target: { value: "Refunds are processed within 5 days." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(requests.some((r) => r.method === "POST")).toBe(true));
    const post = requests.find((r) => r.method === "POST");
    expect(post?.url).toBe("/api/v1/knowledge-base");
    expect(JSON.parse(post?.body ?? "{}")).toMatchObject({
      title: "Refunds",
      content: "Refunds are processed within 5 days.",
    });
  });

  it("keeps Save disabled until there is a title and content", async () => {
    stubApi();
    renderKnowledge();

    (await screen.findByRole("button", { name: /Add knowledge/ })).click();

    const save = (await screen.findByRole("button", { name: "Save" })) as HTMLButtonElement;
    expect(save.disabled).toBe(true);

    fireEvent.change(await screen.findByLabelText("Title"), { target: { value: "Refunds" } });
    expect((screen.getByRole("button", { name: "Save" }) as HTMLButtonElement).disabled).toBe(
      true,
    );

    fireEvent.change(await screen.findByLabelText("Content"), { target: { value: "Some text" } });
    expect((screen.getByRole("button", { name: "Save" }) as HTMLButtonElement).disabled).toBe(
      false,
    );
  });

  it("warns when a document is too short to retrieve against", async () => {
    stubApi({ items: [{ ...DOCUMENT, title: "Tiny", chunk_count: 1 }] });
    renderKnowledge();

    expect(await screen.findByText(/hard to retrieve against/)).toBeDefined();
  });

  it("gives every document a labelled delete control, not an unlabelled icon", async () => {
    stubApi();
    renderKnowledge();

    const remove = await screen.findByRole("button", { name: /Delete Pricing FAQ/ });
    expect(remove).toBeDefined();
  });

  it("explains a failed load", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 500 })));
    renderKnowledge();

    expect(await screen.findByText("Could not load your knowledge")).toBeDefined();
  });
});
