import { describe, expect, it, vi } from "vitest";

/**
 * The root route is a redirect, not a page.
 *
 * Worth a test because the failure it prevents is invisible: if `/` rendered its own copy
 * of the dashboard, nothing would break - there would simply be two dashboards, and they
 * would drift. Asserting the redirect is what keeps the product's front door singular.
 */
vi.mock("next/navigation", () => ({
  redirect: vi.fn(),
}));

import { redirect } from "next/navigation";

import Home from "@/app/page";

describe("/ (Home)", () => {
  it("sends the visitor to the dashboard", () => {
    Home();
    expect(redirect).toHaveBeenCalledWith("/dashboard");
  });
});
