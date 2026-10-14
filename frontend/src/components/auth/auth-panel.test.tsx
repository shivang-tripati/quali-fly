import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AuthPanel, type AuthMode } from "@/components/auth/auth-panel";
import { Providers } from "@/components/providers";

const push = vi.fn();
const replace = vi.fn();
const linkTargets: string[] = [];

/**
 * `next/link` is mocked so a test can assert *where* the cross-link points without
 * standing up a router. jsdom does not navigate on click anyway, so the href is the only
 * observable fact about it - and the href is the thing that matters here, because "sign-in
 * and sign-up are real, linkable routes" is the requirement being pinned.
 */
vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => {
    linkTargets.push(href);
    return <a href={href} {...rest}>{children}</a>;
  },
}));

vi.mock("next/navigation", () => ({
  usePathname: () => "/signin",
  useRouter: () => ({ push, replace }),
  useSearchParams: () => new URLSearchParams(),
}));

const USER = {
  id: "aaaa1111-1111-4111-8111-111111111111",
  email: "sam@acme.test",
  name: "Sam",
  is_active: true,
};
const WORKSPACE = {
  id: "bbbb1111-1111-4111-8111-111111111111",
  name: "Acme Auto Sales",
  slug: "acme-auto-sales",
};

/**
 * A backend that is real in the ways that matter and fake nowhere else.
 *
 * `registerStatus` is the one behaviour under test that cannot be produced by a happy
 * response - what the form does when the server says the address is taken. Everything else
 * is the actual contract: the request body, the response shape, and the follow-up
 * `/auth/me` read.
 */
function stubApi({
  registerStatus = 201,
  errorBody,
}: { registerStatus?: number; errorBody?: unknown } = {}) {
  const calls: { url: string; body: unknown }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const body = init?.body ? JSON.parse(String(init.body)) : undefined;
      calls.push({ url, body });

      if (url.includes("/api/v1/auth/register")) {
        if (registerStatus !== 201) {
          return new Response(JSON.stringify(errorBody), {
            status: registerStatus,
            headers: { "Content-Type": "application/json" },
          });
        }
        return new Response(JSON.stringify({ user: USER, workspace: WORKSPACE, role: "OWNER" }), {
          status: 201,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.includes("/api/v1/auth/login")) {
        return new Response(JSON.stringify({ user: USER }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.includes("/api/v1/auth/me")) {
        return new Response(JSON.stringify(USER), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      return new Response("{}", { headers: { "Content-Type": "application/json" } });
    }),
  );
  return calls;
}

function renderPanel(mode: AuthMode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <Providers client={client}>
      <AuthPanel mode={mode} />
    </Providers>,
  );
}

/** Drive the real form rather than a hand-built one, so the submit handler is exercised. */
async function fillSignup(overrides: Partial<Record<string, string>> = {}) {
  const values = {
    "Your name": "Sam Rivera",
    "Workspace name": "Acme Auto Sales",
    Email: "sam@acme.test",
    Password: "a-long-enough-password",
    ...overrides,
  };
  for (const [label, value] of Object.entries(values)) {
    fireEvent.change(screen.getByLabelText(label), { target: { value } });
  }
}

function signUpPayload(calls: { url: string; body: unknown }[]) {
  const call = calls.find((c) => c.url.includes("/auth/register"));
  return call?.body as Record<string, string> | undefined;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  push.mockClear();
  replace.mockClear();
  linkTargets.length = 0;
  window.localStorage.clear();
});

describe("AuthPanel", () => {
  it("points each mode at the other route, so both entry points are linkable", () => {
    // The requirement that made these routes instead of a mode toggle. If the cross-link
    // stopped being a real href, sign-in would again be reachable only by rendering some
    // other page, and could not be linked to from a landing page or a support answer.
    stubApi();

    const signin = renderPanel("signin");
    expect(screen.getByRole("link", { name: "Create an account" }).getAttribute("href")).toBe(
      "/signup",
    );
    signin.unmount();

    renderPanel("signup");
    expect(screen.getByRole("link", { name: "Sign in" }).getAttribute("href")).toBe("/signin");
  });

  it("shows only the fields the route asks for", () => {
    stubApi();

    const signin = renderPanel("signin");
    expect(screen.getByRole("heading", { name: "Sign in" })).toBeDefined();
    expect(screen.queryByLabelText("Workspace name")).toBeNull();
    expect(screen.queryByLabelText("Your name")).toBeNull();
    signin.unmount();

    renderPanel("signup");
    expect(screen.getByRole("heading", { name: "Create your account" })).toBeDefined();
    expect(screen.getByLabelText("Workspace name")).toBeDefined();
    expect(screen.getByLabelText("Your name")).toBeDefined();
  });

  it("signs a new customer up and sends them to onboarding", async () => {
    const calls = stubApi();
    renderPanel("signup");
    await fillSignup();

    fireEvent.click(screen.getByRole("button", { name: "Create account" }));

    // The request carries exactly the four fields the form collects. Anything more would be
    // the client choosing an identity it has no business choosing.
    await waitFor(() => expect(signUpPayload(calls)).toBeDefined());
    expect(Object.keys(signUpPayload(calls)!).sort()).toEqual([
      "email",
      "name",
      "password",
      "workspace_name",
    ]);
    expect(signUpPayload(calls)).toMatchObject({
      name: "Sam Rivera",
      email: "sam@acme.test",
      workspace_name: "Acme Auto Sales",
    });

    await waitFor(() => expect(push).toHaveBeenCalledWith("/welcome"));
  });

  it("selects the new workspace so tenant-scoped requests work immediately", async () => {
    stubApi();
    renderPanel("signup");
    await fillSignup();
    fireEvent.click(screen.getByRole("button", { name: "Create account" }));

    // Without this the first requests go out with no `X-Tenant-ID` and the API answers
    // TENANT_REQUIRED, so a brand-new customer lands on a screen of errors.
    await waitFor(() =>
      expect(window.localStorage.getItem("qualifly.selectedTenant")).toBe(WORKSPACE.id),
    );
  });

  it("shows the server's message when the email is already registered", async () => {
    stubApi({
      registerStatus: 409,
      errorBody: {
        error: {
          code: "EMAIL_TAKEN",
          message: "An account with this email already exists. Sign in instead.",
        },
      },
    });
    renderPanel("signup");
    await fillSignup();
    fireEvent.click(screen.getByRole("button", { name: "Create account" }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("already exists");
    // A failed signup must not navigate: the customer has no account yet.
    expect(push).not.toHaveBeenCalled();
  });

  it("states the password rule before submitting rather than after", () => {
    stubApi();
    renderPanel("signup");

    expect(screen.getByText(/At least 8 characters/)).toBeDefined();
  });

  it("asks for the new-password autocomplete on signup and current-password on sign-in", () => {
    // A password manager filling the *old* password field on a signup form is a real and
    // confusing failure, and this is the attribute that prevents it.
    stubApi();

    const signin = renderPanel("signin");
    expect(screen.getByLabelText("Password").getAttribute("autocomplete")).toBe("current-password");
    signin.unmount();

    renderPanel("signup");
    expect(screen.getByLabelText("Password").getAttribute("autocomplete")).toBe("new-password");
  });

  it("waits for the session to be readable before treating sign-in as complete", async () => {
    // The regression this guards. Roughly one sign-in in six answers the first `/auth/me`
    // with 401 even though the session row is already committed and visible to an
    // independent connection - a transient read failure, not a commit-ordering problem.
    // `retry: false` on the identity queries caches that 401 as a hard answer, which bounces
    // the user straight back to the sign-in form after a login the server accepted.
    // The backend guarantee itself is pinned by backend/tests/test_login_boundary.py.
    let meCalls = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/v1/auth/login")) {
          return new Response(JSON.stringify({ user: USER }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          });
        }
        if (url.includes("/api/v1/auth/me")) {
          meCalls += 1;
          if (meCalls <= 2) {
            return new Response(
              JSON.stringify({ error: { code: "AUTH_REQUIRED", message: "x" } }),
              { status: 401, headers: { "Content-Type": "application/json" } },
            );
          }
          return new Response(JSON.stringify(USER), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          });
        }
        return new Response("{}", { headers: { "Content-Type": "application/json" } });
      }),
    );

    renderPanel("signin");
    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "sam@acme.test" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "hunter2hunter2" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => expect(meCalls).toBeGreaterThanOrEqual(3), { timeout: 5000 });
    // The point of the test: more than one probe. A client that fired once and gave up on
    // the first 401 would leave the user staring at the sign-in form after a login the
    // server had actually accepted.
    expect(meCalls).toBeGreaterThan(1);
  }, 10000);

  it("still signs an existing user in", async () => {
    // The regression this guards: adding the routes must not disturb the one working entry
    // path into the product.
    const calls = stubApi();
    renderPanel("signin");

    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "sam@acme.test" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "hunter2hunter2" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => expect(calls.some((c) => c.url.includes("/api/v1/auth/login"))).toBe(true));
    // Sign-in must not be pushed to onboarding: an existing customer is not new.
    expect(push).not.toHaveBeenCalled();
  });
});
