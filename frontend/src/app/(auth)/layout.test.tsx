import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import AuthGroupLayout from "@/app/(auth)/layout";

/**
 * The regression that motivated splitting the layouts.
 *
 * The sign-in form used to render *inside* the application shell, so a visitor creating an
 * account was shown a sidebar, a workspace switcher and a notification bell. Beyond looking
 * wrong, every one of those controls fetches tenant-scoped data, so the screen was also
 * driving guaranteed `TENANT_ACCESS_DENIED` requests for someone who has no workspace.
 *
 * Asserted as absence rather than presence: a test that only checks the form renders would
 * have passed against the old layout, which also rendered the form.
 */
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function renderAuth() {
  return render(
    <AuthGroupLayout params={Promise.resolve({})}>
      <p>the form</p>
    </AuthGroupLayout>,
  );
}

describe("(auth) layout", () => {
  it("renders the page inside it", () => {
    renderAuth();
    expect(screen.getByText("the form")).toBeDefined();
  });

  it("has no primary navigation", () => {
    renderAuth();
    expect(screen.queryByRole("navigation", { name: "Main" })).toBeNull();
    for (const label of ["Dashboard", "Handoffs", "Conversations", "Leads", "Follow-ups"]) {
      expect(screen.queryByRole("link", { name: label })).toBeNull();
    }
  });

  it("has no workspace switcher, notification bell or command palette", () => {
    // Each of these is a control that cannot work for someone who is not signed in, and
    // each one costs a tenant-scoped request on mount.
    renderAuth();
    expect(screen.queryByLabelText("Workspace")).toBeNull();
    expect(screen.queryByRole("button", { name: /notifications/i })).toBeNull();
    expect(screen.queryByRole("button", { name: "Search" })).toBeNull();
    expect(screen.queryByText(/Your workspace yet/i)).toBeNull();
  });

  it("offers no route into the signed-in product", () => {
    // An auth screen that links to the dashboard invites a signed-out visitor to collect
    // four 403s instead of telling them to sign in.
    renderAuth();
    const hrefs = screen
      .getAllByRole("link")
      .map((link) => link.getAttribute("href"));
    expect(hrefs).not.toContain("/dashboard");
    expect(hrefs).not.toContain("/leads");
  });

  it("keeps the wordmark as a link home, so the brand is a destination", () => {
    renderAuth();
    const brand = screen.getByRole("link", { name: "QualiFly" });
    expect(brand.getAttribute("href")).toBe("/");
  });
});
