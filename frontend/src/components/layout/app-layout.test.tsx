import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Sparkles } from "lucide-react";

import { AppLayout } from "@/components/layout/app-layout";
import { SidebarNav } from "@/components/layout/sidebar-nav";
import { Providers } from "@/components/providers";

let pathname = "/dashboard";
const replace = vi.fn();
const push = vi.fn();

vi.mock("next/navigation", () => ({
  usePathname: () => pathname,
  useRouter: () => ({ replace, push }),
  useSearchParams: () => new URLSearchParams(),
}));

const USER = { id: "aaaa1111-1111-4111-8111-111111111111", email: "sam@acme.test", name: "Sam", is_active: true };
const TENANT = { id: "bbbb1111-1111-4111-8111-111111111111", name: "Acme Auto Sales", slug: "acme", role: "OWNER" };
const SECOND_TENANT = { id: "cccc1111-1111-4111-8111-111111111111", name: "Globex", slug: "globex", role: "MEMBER" };

// Mutable so a test can put the shell in the state a first-time visitor is actually in:
// authenticated, in more than one workspace, with nothing selected yet.
const auth = {
  tenants: [TENANT] as typeof TENANT[],
  tenantId: TENANT.id as string | null,
  selectTenant: vi.fn(),
  isAuthenticated: true,
  // Mutable because "we have not asked yet" and "we asked and the answer was no" both leave
  // `isAuthenticated` false, and the shell must treat them differently. Redirecting on the
  // first would sign out every real user on every hard refresh.
  isLoading: false,
};

vi.mock("@/lib/auth/hooks", () => ({
  useCurrentUser: () => ({
    user: auth.isAuthenticated ? USER : null,
    isLoading: auth.isLoading,
    isAuthenticated: auth.isAuthenticated,
  }),
  useTenants: () => ({ tenants: auth.tenants, isLoading: false }),
  useCurrentTenant: () => ({ tenantId: auth.tenantId, selectTenant: auth.selectTenant }),
}));

function dashboard(overrides: Record<string, unknown> = {}) {
  return {
    summary: {
      new_leads: 0,
      new_leads_today: 0,
      qualified_leads: 0,
      handoffs_open: 0,
      handoffs_unassigned: 0,
      handoffs_mine: 0,
      follow_ups_due: 0,
      follow_ups_overdue: 0,
      follow_ups_upcoming: 0,
      knowledge_documents: 0,
    },
    attention: [],
    lead_activity: { new: 0, qualified: 0, replying: 0, handoff: 0, closed: 0 },
    recent_conversations: [],
    integration: null,
    generated_at: "2026-09-29T09:00:00Z",
    ...overrides,
  };
}

function stubApi(body: unknown = dashboard()) {
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      calls.push(url);
      if (url.includes("/api/v1/dashboard")) {
        return new Response(JSON.stringify(body), {
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.includes("/notifications/inbox")) {
        return new Response(JSON.stringify({ items: [], count: 0 }), {
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.includes("/api/v1/leads")) {
        return new Response(JSON.stringify({ items: [], count: 0, total: 0 }), {
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.includes("/api/v1/handoffs")) {
        return new Response(JSON.stringify({ items: [], count: 0 }), {
          headers: { "Content-Type": "application/json" },
        });
      }
      return new Response("{}", { headers: { "Content-Type": "application/json" } });
    }),
  );
  return calls;
}

function renderShell(children: React.ReactNode = <p>page</p>) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <Providers client={client}>
      <AppLayout>{children}</AppLayout>
    </Providers>,
  );
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  pathname = "/dashboard";
  push.mockClear();
  replace.mockClear();
  auth.tenants = [TENANT];
  auth.tenantId = TENANT.id;
  auth.isAuthenticated = true;
  auth.isLoading = false;
  auth.selectTenant.mockClear();
});

describe("SidebarNav", () => {
  it("marks the active route for assistive technology, not just by colour", () => {
    pathname = "/handoffs";
    render(
      <Providers client={new QueryClient()}>
        <SidebarNav />
      </Providers>,
    );
    const current = screen.getByRole("link", { name: "Handoffs" });
    expect(current.getAttribute("aria-current")).toBe("page");
    expect(screen.getByRole("link", { name: "Dashboard" }).getAttribute("aria-current")).toBeNull();
  });

  it("marks a nested route as active too", () => {
    pathname = "/leads/7c2504e0-4f89-41d3-9a0c-0305e82c3301";
    render(
      <Providers client={new QueryClient()}>
        <SidebarNav />
      </Providers>,
    );
    expect(
      screen.getByRole("link", { name: "Leads" }).getAttribute("aria-current"),
    ).toBe("page");
  });

  it("shows a waiting count with words, not just a coloured dot", () => {
    const client = new QueryClient();
    render(
      <Providers client={client}>
        <SidebarNav
          items={[
            { href: "/handoffs", label: "Handoffs", icon: Sparkles, count: 3, countLabel: "waiting for a person" },
          ]}
        />
      </Providers>,
    );
    expect(screen.getByText("3")).toBeDefined();
    expect(screen.getByText("waiting for a person")).toBeDefined();
  });

  it("omits the badge entirely at zero, so a quiet queue looks quiet", () => {
    const client = new QueryClient();
    render(
      <Providers client={client}>
        <SidebarNav
          items={[
            { href: "/handoffs", label: "Handoffs", icon: Sparkles, count: 0 },
          ]}
        />
      </Providers>,
    );
    expect(screen.queryByText("0")).toBeNull();
  });
});

describe("AppLayout", () => {
  it("renders the shell and the page inside it", () => {
    stubApi();
    renderShell(<p>the page</p>);
    expect(screen.getByText("the page")).toBeDefined();
    expect(screen.getByRole("navigation", { name: "Main" })).toBeDefined();
  });

  it("adopts a workspace when none is selected, or every request is rejected", async () => {
    // The regression this guards. The store only ever held what a person explicitly picked,
    // so on a first visit the switcher *displayed* the first workspace as active while the
    // request layer still had no tenant. Every tenant-scoped call then went out without
    // `X-Tenant-ID` and came back TENANT_REQUIRED, so the whole product rendered as an
    // empty screen with an error on it.
    auth.tenants = [TENANT, SECOND_TENANT];
    auth.tenantId = null;
    stubApi();

    renderShell(<p>the page</p>);

    await waitFor(() => expect(auth.selectTenant).toHaveBeenCalledWith(TENANT.id));
    // The first membership, which is also the one the switcher was already showing.
    expect(auth.selectTenant).not.toHaveBeenCalledWith(SECOND_TENANT.id);
  });

  it("does not fetch tenant-scoped data before a workspace is resolved", () => {
    // Otherwise the page mounts on the render *before* the effect writes the store, its
    // queries fire once with no tenant header, and the user sees a guaranteed-400 error
    // before the retry succeeds.
    auth.tenants = [TENANT, SECOND_TENANT];
    auth.tenantId = null;
    const calls = stubApi();

    renderShell(<p>the page</p>);

    expect(calls.some((url) => url.includes("/api/v1/dashboard"))).toBe(false);
  });

  it("leaves an existing workspace selection alone", async () => {
    auth.tenants = [TENANT, SECOND_TENANT];
    auth.tenantId = SECOND_TENANT.id;
    stubApi();

    renderShell(<p>the page</p>);

    await waitFor(() => expect(screen.getByText("the page")).toBeDefined());
    expect(auth.selectTenant).not.toHaveBeenCalled();
  });

  it("does not block a user who belongs to no workspace at all", () => {
    auth.tenants = [];
    auth.tenantId = null;
    stubApi();

    renderShell(<p>the page</p>);

    // Otherwise a user with zero memberships waits on a skeleton forever.
    expect(screen.getByText("the page")).toBeDefined();
    expect(auth.selectTenant).not.toHaveBeenCalled();
  });

  it("shows the workspace identity", async () => {
    stubApi();
    renderShell();
    expect(await screen.findByText("Acme Auto Sales")).toBeDefined();
  });

  it("badges the sidebar with the unassigned handoff count", async () => {
    stubApi(
      dashboard({
        summary: {
          new_leads: 0,
          new_leads_today: 0,
          qualified_leads: 0,
          handoffs_open: 3,
          handoffs_unassigned: 2,
          handoffs_mine: 0,
          follow_ups_due: 0,
          follow_ups_overdue: 0,
          follow_ups_upcoming: 0,
          knowledge_documents: 0,
        },
      }),
    );
    renderShell();

    await waitFor(() =>
      expect(screen.getByRole("link", { name: /Handoffs/ }).textContent).toContain("2"),
    );
  });

  it("opens the command palette with the keyboard shortcut", async () => {
    stubApi();
    renderShell();

    expect(screen.queryByRole("dialog", { name: "Search" })).toBeNull();

    fireEvent.keyDown(document, { key: "k", metaKey: true });
    expect(await screen.findByRole("dialog", { name: "Search" })).toBeDefined();

    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Search" })).toBeNull());
  });

  it("does not search on one character, because that is a file browser", async () => {
    const calls = stubApi();
    renderShell();
    fireEvent.keyDown(document, { key: "k", ctrlKey: true });

    fireEvent.change(await screen.findByLabelText("Search leads and handoffs"), {
      target: { value: "p" },
    });

    await waitFor(() =>
      expect(calls.some((url) => url.includes("/api/v1/leads"))).toBe(false),
    );
    expect(screen.getByText(/Type at least two characters/)).toBeDefined();
  });

  it("searches the handoff queue too, and links the result to that handoff", async () => {
    const handoffId = "55555555-5555-4555-8555-555555555555";
    const calls = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/v1/handoffs") && url.includes("search=Dana")) {
        return new Response(
          JSON.stringify({
            items: [
              {
                id: handoffId,
                conversation_id: "6ba7b810-9dad-41d1-80b4-00c04fd430c8",
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
                lead_name: "Dana Buyer",
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
      if (url.includes("/api/v1/handoffs") || url.includes("/api/v1/leads")) {
        return new Response(JSON.stringify({ items: [], count: 0, total: 0 }), {
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.includes("/api/v1/dashboard")) {
        return new Response(JSON.stringify(dashboard()), {
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.includes("/notifications/inbox")) {
        return new Response(JSON.stringify({ items: [], count: 0 }), {
          headers: { "Content-Type": "application/json" },
        });
      }
      return new Response("{}", { headers: { "Content-Type": "application/json" } });
    });
    vi.stubGlobal("fetch", calls);
    renderShell();

    fireEvent.keyDown(document, { key: "k", ctrlKey: true });
    fireEvent.change(await screen.findByLabelText("Search leads and handoffs"), {
      target: { value: "Dana" },
    });

    // A real server-side query, and a result that goes to the handoff rather than a lead.
    expect(await screen.findByText("Dana Buyer")).toBeDefined();
    expect(
      calls.mock.calls.some(([url]) => String(url).includes("/api/v1/handoffs?search=Dana")),
    ).toBe(true);
    fireEvent.click(screen.getByText("Dana Buyer"));
    expect(push).toHaveBeenCalledWith(`/handoffs?focus=${handoffId}`);
  });

  it("opens the mobile drawer and closes it on navigation", async () => {
    stubApi();
    renderShell();

    fireEvent.click(screen.getByRole("button", { name: "Open navigation" }));
    // The drawer replaces the desktop nav on a small screen rather than squeezing it.
    expect(screen.getAllByRole("navigation", { name: "Main" }).length).toBeGreaterThan(1);

    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() =>
      expect(screen.getAllByRole("navigation", { name: "Main" }).length).toBe(1),
    );
  });

  it("offers a way in when there is no session, rather than a blank page", () => {
    // Signed out, this shell is no longer the sign-in screen - `/signin` has its own layout
    // with no shell on it. What the shell must do instead is get out of the way.
    // The regression: rendering the sign-in form here put a sidebar, a workspace switcher
    // and a notification bell on the sign-up screen, each of which then fetched
    // tenant-scoped data for someone with no workspace.
    vi.resetModules();
    auth.isAuthenticated = false;
    auth.tenantId = null;
    const calls = stubApi();
    renderShell(<p>secret page</p>);

    expect(replace).toHaveBeenCalledWith("/signin");
    // None of the product's destinations should be reachable from here.
    expect(screen.queryByRole("link", { name: "Dashboard" })).toBeNull();
    expect(screen.queryByRole("navigation", { name: "Main" })).toBeNull();
    // And the protected page must not have rendered behind the redirect.
    expect(screen.queryByText("secret page")).toBeNull();
    // The only requests that should have gone out are the identity ones.
    expect(
      calls
        .filter((url) => !url.includes("/auth/me") && !url.includes("/tenants"))
        .filter((url) => !url.includes("/notifications")),
    ).toEqual([]);
  });

  it("does not redirect before the session question is answered", async () => {
    // The race this guards. `isAuthenticated` is false while `/auth/me` is still in flight,
    // so redirecting on `!isAuthenticated` alone would sign out every real user on every
    // hard refresh.
    //
    // Only the redirect is asserted. The page is deliberately *not* rendered in this state
    // - `loading` covers it, because every query inside would otherwise fire with no
    // session - and asserting its absence here would be asserting the wrong thing.
    auth.isLoading = true;
    auth.isAuthenticated = false;
    stubApi();
    renderShell(<p>page</p>);

    // Let the effect that would redirect have run, and confirm it did not.
    await waitFor(() => expect(screen.getByText("QualiFly")).toBeDefined());
    expect(replace).not.toHaveBeenCalled();
  });
});

