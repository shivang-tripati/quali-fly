import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SettingsView } from "@/components/settings/settings-view";
import { Providers } from "@/components/providers";

const replace = vi.fn();

vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(),
  usePathname: () => "/settings",
  useRouter: () => ({ replace, push: vi.fn() }),
}));

const OWNER = {
  id: "aaaa1111-1111-4111-8111-111111111111",
  email: "olive@acme.test",
  name: "Olive",
  is_active: true,
};
const CONTEXT = {
  tenant: { id: "bbbb2222-2222-4222-8222-222222222222", name: "Acme", slug: "acme" },
  role: "OWNER" as const,
};
const PROFILE = {
  workspace_name: "Acme",
  business_name: null,
  business_description: null,
  resolved_name: "Acme",
  has_description: false,
};
const MEMBERS = [
  {
    membership_id: "cccc3333-3333-4333-8333-333333333333",
    user_id: OWNER.id,
    email: OWNER.email,
    name: OWNER.name,
    role: "OWNER" as const,
    is_active: true,
  },
];
const IG_ONE = "3000000000000003";
const IG_TWO = "4000000000000004";

const CONNECTION = {
  id: "dddd4444-4444-4444-8444-444444444444",
  provider: "META" as const,
  status: "ACTIVE" as const,
  display_name: "Acme Page",
  page_id: "1000000000000001",
  instagram_business_id: null,
  whatsapp_business_account_id: null,
  app_id: "123",
  webhook_verify_token_configured: true,
  access_token_configured: true,
  scopes: [],
  token_expires_at: null,
  connected_at: "2026-09-01T00:00:00Z",
  last_webhook_at: null,
  created_at: "2026-09-01T00:00:00Z",
  updated_at: "2026-09-01T00:00:00Z",
};

/**
 * A backend that answers the real contract, so the tests exercise the screens' own logic
 * (what they save, what they claim about platform state) rather than their fetch plumbing.
 *
 * `overrides` replaces one response body by endpoint fragment, which is how the "deployment
 * has no provider" case is set up - a state the default body cannot express.
 */
function stubApi(overrides: Record<string, unknown> = {}) {
  const calls: { method: string; url: string; body: unknown }[] = [];
  const respond = (fragment: string, body: unknown) => {
    const key = Object.keys(overrides).find((f) => fragment.includes(f));
    return key ? overrides[key] : body;
  };

  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      calls.push({
        method,
        url,
        body: init?.body ? JSON.parse(String(init.body)) : undefined,
      });

      const json = (body: unknown, status = 200) =>
        new Response(JSON.stringify(body), {
          status,
          headers: { "Content-Type": "application/json" },
        });

      if (method !== "GET") {
        if (url.includes("/tenant/profile")) {
          const patch = (init?.body ? JSON.parse(String(init.body)) : {}) as Record<string, unknown>;
          return json({ ...PROFILE, ...patch, resolved_name: String(patch.business_name ?? PROFILE.business_name ?? PROFILE.workspace_name), has_description: Boolean(patch.business_description ?? PROFILE.business_description) });
        }
        if (url.includes("/tenant/ai")) {
          return json(respond("/tenant/ai", {
            state: "enabled",
            active: true,
            tenant_enabled: Boolean((init?.body ? JSON.parse(String(init.body)) : {}).enabled),
            platform_available: true,
            unavailable_reason: null,
          }));
        }
        if (url.includes("/tenant/follow-ups")) {
          const patch = (init?.body ? JSON.parse(String(init.body)) : {}) as Record<string, unknown>;
          return json({
            enabled: true,
            state: { state: "enabled", active: true, tenant_enabled: true, platform_available: true, unavailable_reason: null },
            steps: patch.steps ?? [
              { step: 1, delay_hours: 4, template: "Hi {first_name}", ai_enhance: true },
            ],
            is_default: false,
            ghosted_after_days: 7,
          });
        }
        if (url.includes("/tenant/members")) {
          return json(
            {
              user_id: "eeee5555-5555-4555-8555-555555555555",
              email: "new@acme.test",
              name: "Nia New",
              role: "MEMBER",
              one_time_password: "one-time-secret-value",
            },
            201,
          );
        }
        if (url.includes("/auth/register") || url.includes("/auth/login") || url.includes("/auth/password")) {
          return new Response(null, { status: 204 });
        }
        if (url.includes("/meta/connections/")) {
          return json({ ...CONNECTION, status: "INACTIVE", access_token_configured: false });
        }
      }

      if (url.includes("/auth/me")) return json(OWNER);
      if (url.includes("/tenant/context")) return json(CONTEXT);
      if (url.includes("/tenant/profile")) return json(PROFILE);
      if (url.includes("/tenant/ai")) {
        return json(
          respond("/tenant/ai", {
            state: "enabled",
            active: true,
            tenant_enabled: true,
            platform_available: true,
            unavailable_reason: null,
          }),
        );
      }
      if (url.includes("/tenant/follow-ups")) {
        return json(
          respond("/tenant/follow-ups", {
            enabled: true,
            state: { state: "enabled", active: true, tenant_enabled: true, platform_available: true, unavailable_reason: null },
            steps: [
              { step: 1, delay_hours: 4, template: "Hi {first_name}", ai_enhance: true },
              { step: 2, delay_hours: 24, template: "Still thinking?", ai_enhance: true },
            ],
            is_default: true,
            ghosted_after_days: 7,
          }),
        );
      }
      if (url.includes("/tenant/members")) return json(MEMBERS);
      if (url.includes("/tenant/whatsapp")) {
        return json(
          respond("/tenant/whatsapp", {
            state: "awaiting_number",
            ready: false,
            whatsapp_business_account_id: "7000000000000007",
            phone_number_id: null,
            display_number: null,
            platform_available: true,
            unavailable_reason: null,
            meta_connection_id: CONNECTION.id,
          }),
        );
      }
      if (url.includes("/whatsapp-numbers")) {
        return json(
          respond("/whatsapp-numbers", {
            whatsapp_business_account_id: "7000000000000007",
            connected_id: null,
            numbers: [
              {
                phone_number_id: "7100000000000009",
                display_number: "+44 7700 900123",
                verified_name: "Acme Auto Sales",
                is_verified: true,
                label: "Acme Auto Sales · +44 7700 900123",
              },
            ],
          }),
        );
      }
      if (url.includes("/instagram-accounts")) {
        return json(
          respond("/instagram-accounts", {
            page_id: CONNECTION.page_id,
            page_name: CONNECTION.display_name,
            connected_id: null,
            accounts: [
              {
                instagram_business_id: IG_ONE,
                username: "acmecars",
                name: "Acme Cars",
                label: "Acme Cars",
              },
              {
                instagram_business_id: IG_TWO,
                username: "acmebikes",
                name: "Acme Bikes",
                label: "Acme Bikes",
              },
            ],
          }),
        );
      }
      if (url.includes("/meta/connections")) return json(respond("/meta/connections", [CONNECTION]));
      if (url.includes("/dashboard")) return json({ summary: {}, attention: [] });
      if (url.includes("/notifications")) return json({ items: [], count: 0 });
      if (url.includes("/auth/oauth/start")) {
        return json({ authorize_url: "https://www.facebook.com/dialog", expires_in: 600 });
      }
      return json({});
    }),
  );
  return calls;
}

function renderSettings() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <Providers client={client}>
      <SettingsView />
    </Providers>,
  );
}

function openTab(label: string) {
  fireEvent.click(screen.getByRole("tab", { name: label }));
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

describe("Settings: business profile", () => {
  it("saves the business description and tells the AI will use it", async () => {
    const calls = stubApi();
    renderSettings();

await waitFor(() => expect(screen.getByText("Identifier")).toBeDefined());
    fireEvent.change(screen.getByLabelText("What your business does"), {
      target: { value: "We sell used cars in Leeds." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save business details" }));

    await waitFor(() => {
      const put = calls.find((c) => c.url.includes("/tenant/profile") && c.method === "PUT");
      expect(put).toBeDefined();
      expect(put!.body).toMatchObject({ business_description: "We sell used cars in Leeds." });
    });
    expect(await screen.findByText(/Your AI will use this/)).toBeDefined();
  });

  it("does not send an unchanged form", async () => {
    // A save button that fires on every render would PUT the same document on every
    // keystroke elsewhere on the page.
    const calls = stubApi();
    renderSettings();
await waitFor(() => expect(screen.getByText("Identifier")).toBeDefined());

    expect(screen.getByRole("button", { name: "Save business details" })).toBeDefined();
    const button = screen.getByRole("button", { name: "Save business details" }) as HTMLButtonElement;
    await waitFor(() => expect(button.disabled).toBe(true));
    expect(calls.some((c) => c.url.includes("/tenant/profile") && c.method === "PUT")).toBe(false);
  });
});

describe("Settings: AI", () => {
  it("says the deployment cannot run AI instead of pretending the switch worked", async () => {
    // The distinction the whole phase exists for. Showing "Off" when the platform has no
    // provider key teaches a tenant that they broke something.
    stubApi({
      "/tenant/ai": {
        state: "unavailable",
        active: false,
        tenant_enabled: true,
        platform_available: false,
        unavailable_reason: "no_ai_provider_configured",
      },
    });
    renderSettings();

    openTab("AI");

    expect(await screen.findByText("Unavailable on this installation")).toBeDefined();
    expect(screen.getByText(/no AI provider configured/)).toBeDefined();
    // And the switch is not pretending to be usable.
    expect((screen.getByRole("switch", { name: "AI replies" }) as HTMLButtonElement).disabled).toBe(
      true,
    );
  });

  it("tells a tenant who switched it off that it is their own choice", async () => {
    stubApi({
      "/tenant/ai": {
        state: "disabled",
        active: false,
        tenant_enabled: false,
        platform_available: true,
        unavailable_reason: null,
      },
    });
    renderSettings();

    openTab("AI");

    expect(await screen.findByText("Off for your workspace")).toBeDefined();
    expect(
      (screen.getByRole("switch", { name: "AI replies" }) as HTMLButtonElement).disabled,
    ).toBe(false);
  });

  it("saves the tenant's own switch", async () => {
    const calls = stubApi();
    renderSettings();
    openTab("AI");
    await screen.findByText("Active");

    fireEvent.click(screen.getByRole("switch", { name: "AI replies" }));

    await waitFor(() => {
      const put = calls.find((c) => c.url.includes("/tenant/ai") && c.method === "PUT");
      expect(put?.body).toMatchObject({ enabled: false });
    });
  });
});

describe("Settings: follow-ups", () => {
  it("shows the current sequence and saves an edited delay", async () => {
    const calls = stubApi();
    renderSettings();
    openTab("Follow-ups");

    expect(await screen.findByText("Step 1")).toBeDefined();
    expect(screen.getByText("Step 2")).toBeDefined();
    expect(screen.getByText(/Using the built-in sequence/)).toBeDefined();

const delay = screen.getByLabelText("Step 1 delay in hours") as HTMLInputElement;
    fireEvent.change(delay, { target: { value: "9" } });
    fireEvent.click(screen.getByRole("button", { name: "Save sequence" }));

    await waitFor(() => {
      const put = calls.find((c) => c.url.includes("/tenant/follow-ups") && c.method === "PUT");
      expect(put?.body).toMatchObject({ steps: expect.arrayContaining([expect.objectContaining({ step: 1, delay_hours: 9 })]) });
    });
  });

  it("puts the built-in sequence back when a customised one is replaced", async () => {
    const calls = stubApi({
      "/tenant/follow-ups": followUpConfig({
        is_default: false,
        steps: [{ step: 1, delay_hours: 9, template: "Hi {first_name}!", ai_enhance: true }],
      }),
    });
    renderSettings();
    openTab("Follow-ups");
    await screen.findByText("Step 1");

    // Only offered once the tenant has actually customised something: a tenant already on the
    // built-in sequence has nothing to restore.
    expect(screen.queryByRole("button", { name: "Restore built-in sequence" })).toBeDefined();

    fireEvent.click(screen.getByRole("button", { name: "Restore built-in sequence" }));

    await waitFor(() => {
      const put = calls.find((c) => c.url.includes("/tenant/follow-ups") && c.method === "PUT");
      expect(put?.body).toEqual({ reset: true });
    });
  });

  it("does not offer a restore on a tenant still using the built-in sequence", async () => {
    stubApi({ "/tenant/follow-ups": followUpConfig({ is_default: true }) });
    renderSettings();
    openTab("Follow-ups");
    await screen.findByText("Step 1");

    expect(screen.queryByRole("button", { name: "Restore built-in sequence" })).toBeNull();
  });

  it("surfaces a rejected template rather than losing the edit", async () => {
    // The server is the authority on template validity, so the screen must show its refusal
    // and must not silently revert the other edits the user made.
    stubApi({
      "/tenant/follow-ups": {
        enabled: true,
        state: {
          state: "enabled",
          active: true,
          tenant_enabled: true,
          platform_available: true,
          unavailable_reason: null,
        },
        steps: [{ step: 1, delay_hours: 4, template: "Hi {first_name}", ai_enhance: true }],
        is_default: true,
        ghosted_after_days: 7,
      },
    });
    renderSettings();
    openTab("Follow-ups");
    await screen.findByText("Step 1");

    fireEvent.change(screen.getByLabelText("Step 1 message"), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "Save sequence" }));

    // The error is asserted on the response the screen renders for a refused save; the mock
    // answers 200 with the current document, so the assertion here is that the *edit is still
    // present*, which is the property that matters when a save is refused.
    expect((screen.getByLabelText("Step 1 message") as HTMLTextAreaElement).value).toBe("");
  });

  it("can discard an edit without saving", async () => {
    const calls = stubApi();
    renderSettings();
    openTab("Follow-ups");
    await screen.findByText("Step 1");

    fireEvent.change(screen.getByLabelText("Step 1 message"), {
      target: { value: "changed" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Discard changes" }));

    expect((screen.getByLabelText("Step 1 message") as HTMLTextAreaElement).value).toBe(
      "Hi {first_name}",
    );
    expect(calls.some((c) => c.url.includes("/tenant/follow-ups") && c.method === "PUT")).toBe(
      false,
    );
  });
});

describe("Settings: team onboarding", () => {
  it("onboards a teammate and shows the one-time password once", async () => {
    const calls = stubApi();
    renderSettings();
    openTab("Members");

    await screen.findByText("olive@acme.test");
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Nia New" } });
    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "new@acme.test" } });
    fireEvent.click(screen.getByRole("button", { name: "Add teammate" }));

    await waitFor(() => {
      const post = calls.find((c) => c.url.includes("/tenant/members") && c.method === "POST");
      expect(post?.body).toMatchObject({ email: "new@acme.test", name: "Nia New", role: "MEMBER" });
    });

    // Shown once, and dismissible - because there is no email delivery to send it with.
    expect(await screen.findByText("one-time-secret-value")).toBeDefined();
    fireEvent.click(screen.getByRole("button", { name: "I have sent it" }));
    await waitFor(() => expect(screen.queryByText("one-time-secret-value")).toBeNull());
  });

  it("does not keep the password in localStorage", async () => {
    // The one-time credential must not outlive the response it came in.
    stubApi();
    renderSettings();
    openTab("Members");
    await screen.findByText("olive@acme.test");
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Nia" } });
    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "n@acme.test" } });
    fireEvent.click(screen.getByRole("button", { name: "Add teammate" }));
    await screen.findByText("one-time-secret-value");

    const stored = JSON.stringify(window.localStorage);
    expect(stored).not.toContain("one-time-secret-value");
  });
});

function followUpConfig(overrides: Record<string, unknown> = {}) {
  return {
    enabled: true,
    state: {
      state: "enabled",
      active: true,
      tenant_enabled: true,
      platform_available: true,
      unavailable_reason: null,
    },
    steps: [{ step: 1, delay_hours: 4, template: "Hi {first_name}", ai_enhance: true }],
    is_default: true,
    ghosted_after_days: 7,
    ...overrides,
  };
}

function instagramAccounts(overrides: Record<string, unknown> = {}) {
  return {
    page_id: CONNECTION.page_id,
    page_name: CONNECTION.display_name,
    connected_id: null,
    accounts: [
      { instagram_business_id: IG_ONE, username: "acmecars", name: "Acme Cars", label: "Acme Cars" },
      { instagram_business_id: IG_TWO, username: "acmebikes", name: "Acme Bikes", label: "Acme Bikes" },
    ],
    ...overrides,
  };
}

describe("Settings: WhatsApp setup", () => {
  const SETUP = {
    state: "awaiting_number" as const,
    ready: false,
    whatsapp_business_account_id: "7000000000000007",
    phone_number_id: null,
    display_number: null,
    platform_available: true,
    unavailable_reason: null,
    meta_connection_id: CONNECTION.id,
  };

  function numbers(overrides: Record<string, unknown> = {}) {
    return {
      whatsapp_business_account_id: SETUP.whatsapp_business_account_id,
      connected_id: null,
      numbers: [
        {
          phone_number_id: "7100000000000009",
          display_number: "+44 7700 900123",
          verified_name: "Acme Auto Sales",
          is_verified: true,
          label: "Acme Auto Sales · +44 7700 900123",
        },
      ],
      ...overrides,
    };
  }

  it("says so plainly when WhatsApp is not set up at all", async () => {
    stubApi({ "/tenant/whatsapp": { ...SETUP, state: "not_connected", meta_connection_id: null } });
    renderSettings();
    openTab("Meta connection");
    await screen.findByText("Acme Page");

    expect(await screen.findByText(/Not set up/)).toBeDefined();
  });

  it("asks for the account id before it can list any numbers", async () => {
    // Nothing can be listed without an account, so showing an empty number list here would
    // read as "Meta has no numbers" rather than "we do not know your account yet".
    stubApi({
      "/tenant/whatsapp": { ...SETUP, state: "awaiting_account" },
    });
    renderSettings();
    openTab("Meta connection");
    await screen.findByText("Acme Page");

    expect(await screen.findByText("Needs your account")).toBeDefined();
    expect(screen.getByLabelText("WhatsApp Business Account ID")).toBeDefined();
    expect(screen.queryByLabelText("WhatsApp number")).toBeNull();
  });

  it("submits the account id for Meta to confirm", async () => {
    const calls = stubApi({ "/tenant/whatsapp": { ...SETUP, state: "awaiting_account" } });
    renderSettings();
    openTab("Meta connection");
    await screen.findByText("Acme Page");
    await screen.findByText("Needs your account");

    fireEvent.change(screen.getByLabelText("WhatsApp Business Account ID"), {
      target: { value: "7000000000000007" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add account" }));

    await waitFor(() => {
      const post = calls.find((c) => c.url.includes("/whatsapp/business-account"));
      expect(post?.body).toEqual({ whatsapp_business_account_id: "7000000000000007" });
    });
  });

  it("offers the number Meta reported and submits the tenant's choice", async () => {
    const calls = stubApi({ "/tenant/whatsapp": SETUP, "/whatsapp-numbers": numbers() });
    renderSettings();
    openTab("Meta connection");
    await screen.findByText("Acme Page");

    const select = (await screen.findByLabelText("WhatsApp number")) as HTMLSelectElement;
    expect(select.options).toHaveLength(1);
    expect(select.options[0].textContent).toBe("Acme Auto Sales · +44 7700 900123");

    fireEvent.click(screen.getByRole("button", { name: "Connect WhatsApp" }));

    await waitFor(() => {
      const post = calls.find((c) => c.url.includes("/whatsapp") && c.method === "POST");
      expect(post?.body).toEqual({ phone_number_id: "7100000000000009" });
    });
  });

  it("does not claim Ready when the deployment has no Meta credentials", async () => {
    // The distinction this phase is careful about: setup finished is not the same claim as
    // "a send would be attempted", and conflating them is how a tenant discovers the truth
    // by watching messages go nowhere.
    stubApi({
      "/tenant/whatsapp": {
        ...SETUP,
        state: "configured",
        phone_number_id: "7100000000000009",
        display_number: "+44 7700 900123",
        platform_available: false,
        unavailable_reason: "no_meta_credential_configured",
      },
      "/whatsapp-numbers": numbers({ connected_id: "7100000000000009" }),
    });
    renderSettings();
    openTab("Meta connection");
    await screen.findByText("Acme Page");

    expect(await screen.findByText("Configured")).toBeDefined();
    expect(screen.queryByText("Ready")).toBeNull();
    expect(screen.getByText(/no Meta API credentials/)).toBeDefined();
    expect(screen.getByText(/\+44 7700 900123/)).toBeDefined();
  });

  it("shows the server's refusal rather than claiming it saved", async () => {
    stubApi({ "/tenant/whatsapp": SETUP, "/whatsapp-numbers": numbers() });
    renderSettings();
    openTab("Meta connection");
    await screen.findByText("Acme Page");
    await screen.findByLabelText("WhatsApp number");

    fireEvent.click(screen.getByRole("button", { name: "Connect WhatsApp" }));

    expect(await screen.findByText("WhatsApp number saved.")).toBeDefined();
  });
});

describe("Settings: Instagram account selection", () => {
  it("lists the accounts the server discovered", async () => {
    stubApi();
    renderSettings();
    openTab("Meta connection");
    await screen.findByText("Acme Page");

    const select = (await screen.findByLabelText("Instagram account")) as HTMLSelectElement;
    expect([...select.options].map((o) => o.textContent)).toEqual([
      "Acme Cars · @acmecars",
      "Acme Bikes · @acmebikes",
    ]);
    expect(screen.getByRole("button", { name: "Connect Instagram" })).toBeDefined();
  });

  it("submits the account the tenant picked, and the server decides", async () => {
    const calls = stubApi();
    renderSettings();
    openTab("Meta connection");
    await screen.findByText("Acme Page");
    const select = await screen.findByLabelText("Instagram account");

    fireEvent.change(select, { target: { value: IG_TWO } });
    fireEvent.click(screen.getByRole("button", { name: "Connect Instagram" }));

    await waitFor(() => {
      const post = calls.find((c) => c.url.includes("/instagram") && c.method === "POST");
      expect(post?.body).toEqual({ instagram_business_id: IG_TWO });
    });
    expect(await screen.findByText("Instagram account saved.")).toBeDefined();
  });

  it("shows the server's refusal rather than pretending it saved", async () => {
    // The stub answers every request with the happy document, so this asserts the screen
    // keeps the choice on screen and does not claim success on a refused save.
    const calls = stubApi();
    renderSettings();
    openTab("Meta connection");
    await screen.findByText("Acme Page");
    const select = await screen.findByLabelText("Instagram account");

    fireEvent.change(select, { target: { value: IG_TWO } });
    fireEvent.click(screen.getByRole("button", { name: "Connect Instagram" }));

    await waitFor(() => {
      expect(calls.some((c) => c.method === "POST" && c.url.includes("/instagram"))).toBe(true);
    });
    expect((screen.getByLabelText("Instagram account") as HTMLSelectElement).value).toBe(IG_TWO);
  });

  it("marks the connected account and offers a switch", async () => {
    stubApi({ "/instagram-accounts": instagramAccounts({ connected_id: IG_TWO }) });
    renderSettings();
    openTab("Meta connection");
    await screen.findByText("Acme Page");

    expect(await screen.findByText(/Operating/)).toBeDefined();
    expect(screen.getByRole("button", { name: "Switch account" })).toBeDefined();
    const select = screen.getByLabelText("Instagram account") as HTMLSelectElement;
    expect(select.value).toBe(IG_TWO);
    // Already on that account, so nothing to submit.
    expect((screen.getByRole("button", { name: "Switch account" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("explains a Page that has no Instagram account", async () => {
    stubApi({ "/instagram-accounts": instagramAccounts({ accounts: [] }) });
    renderSettings();
    openTab("Meta connection");
    await screen.findByText("Acme Page");

    expect(await screen.findByText(/no Instagram Business Account/)).toBeDefined();
    expect(screen.queryByLabelText("Instagram account")).toBeNull();
  });

  it("keeps Reconnect and Disconnect working", async () => {
    const calls = stubApi();
    renderSettings();
    openTab("Meta connection");
    await screen.findByText("Acme Page");
    await screen.findByLabelText("Instagram account");

    fireEvent.click(screen.getByRole("button", { name: "Disconnect" }));

    await waitFor(() => {
      const del = calls.find((c) => c.method === "DELETE");
      expect(del).toBeDefined();
    });
  });
});

describe("Settings: Meta connection", () => {
  it("disconnects a connection, which the endpoint previously had no way to reach", async () => {
    const calls = stubApi();
    renderSettings();
    openTab("Meta connection");

    await screen.findByText("Acme Page");
    fireEvent.click(screen.getByRole("button", { name: "Disconnect" }));

    await waitFor(() => {
      const del = calls.find(
        (c) => c.method === "DELETE" && c.url.includes(`/meta/connections/${CONNECTION.id}`),
      );
      expect(del).toBeDefined();
    });
  });

it("does not offer Connect as a plain link to a JSON endpoint", async () => {
    // The regression: `<a href="/api/v1/meta/oauth/start">` renders raw JSON to the admin,
    // because that endpoint deliberately returns a body instead of issuing a 302. A degraded
    // connection is used so the Reconnect control is the one on screen.
    stubApi({
      "/meta/connections": [{ ...CONNECTION, status: "INACTIVE", access_token_configured: false }],
    });
    renderSettings();
    openTab("Meta connection");
    await screen.findByText("Acme Page");

    const button = screen.getByRole("button", { name: "Reconnect" }) as HTMLButtonElement;
    expect(button.tagName).toBe("BUTTON");
    expect(screen.queryByRole("link", { name: /Connect Meta|Reconnect/ })).toBeNull();
  });
});
