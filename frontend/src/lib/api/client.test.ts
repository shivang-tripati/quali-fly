import { ApiError, TENANT_HEADER, apiRequest } from "@/lib/api/client";
import { TENANT_STORAGE, setSelectedTenantId } from "@/lib/tenant/store";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { z } from "zod";

const payload = z.object({ status: z.string() });

function lastCall() {
  const mock = vi.mocked(fetch);
  const [url, init] = mock.mock.calls.at(-1)!;
  return { url: String(url), init: init as RequestInit };
}

afterEach(() => {
  vi.unstubAllGlobals();
  setSelectedTenantId(null);
});

describe("apiRequest", () => {
  it("validates a successful JSON response", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ status: "ok" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );

    await expect(apiRequest("/health", payload)).resolves.toEqual({ status: "ok" });
  });

  it("turns the backend error envelope into an ApiError", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            error: { code: "TENANT_ACCESS_DENIED", message: "No membership", request_id: "r1" },
          }),
          { status: 403, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );

    const error = await apiRequest("/api/v1/tenant/context", payload).catch((e: unknown) => e);

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 403, code: "TENANT_ACCESS_DENIED", requestId: "r1" });
  });

  it("rejects a response that does not match the schema", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ unexpected: true }), { status: 200 }),
      ),
    );

    await expect(apiRequest("/health", payload)).rejects.toMatchObject({
      code: "invalid_response",
    });
  });

  it("normalises network failures", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockRejectedValue(new TypeError("fetch failed")),
    );

    await expect(apiRequest("/health", payload)).rejects.toMatchObject({
      status: 0,
      code: "network_error",
    });
  });
});

describe("session and tenant credentials", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ status: "ok" }), { status: 200 }),
      ),
    );
  });

  it("always sends the session cookie", async () => {
    await apiRequest("/api/v1/auth/me", payload);

    expect(lastCall().init.credentials).toBe("include");
  });

  it("sends no tenant header when nothing is selected", async () => {
    await apiRequest("/api/v1/tenant/context", payload);

    const headers = lastCall().init.headers as Record<string, string>;
    expect(headers[TENANT_HEADER]).toBeUndefined();
  });

  it("sends the selected tenant header", async () => {
    setSelectedTenantId("3f2504e0-4f89-41d3-9a0c-0305e82c3301");

    await apiRequest("/api/v1/tenant/context", payload);

    const headers = lastCall().init.headers as Record<string, string>;
    expect(headers[TENANT_HEADER]).toBe("3f2504e0-4f89-41d3-9a0c-0305e82c3301");
  });

  it("clears the selected tenant", () => {
    setSelectedTenantId("3f2504e0-4f89-41d3-9a0c-0305e82c3301");
    setSelectedTenantId(null);

    expect(window.localStorage.getItem(TENANT_STORAGE)).toBeNull();
  });
});
