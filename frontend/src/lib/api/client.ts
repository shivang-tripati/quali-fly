import { z } from "zod";

import { env } from "@/lib/env";
import { getSelectedTenantId } from "@/lib/tenant/store";

/** Mirrors the FastAPI error envelope in `app/core/errors.py`. */
export const apiErrorSchema = z.object({
  error: z.object({
    code: z.string(),
    message: z.string(),
    request_id: z.string().optional(),
    details: z.unknown().optional(),
  }),
});

export type ApiErrorBody = z.infer<typeof apiErrorSchema>;

export const TENANT_HEADER = "X-Tenant-ID";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly requestId?: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

const baseUrl = env.NEXT_PUBLIC_API_BASE_URL.replace(/\/+$/, "");

function buildUrl(path: string): string {
  if (/^https?:\/\//i.test(path)) return path;
  return `${baseUrl}${path.startsWith("/") ? path : `/${path}`}`;
}

/**
 * The only place the frontend talks to the backend.
 *
 * Every response is validated with Zod, every non-2xx becomes an `ApiError`, and
 * network failures are normalised too, so callers only handle one error type.
 *
 * `credentials: "include"` carries the session cookie, and the selected tenant is
 * attached as `X-Tenant-ID`. Both are only a *request preference*: the backend
 * re-checks the session and the membership, so a tampered header grants nothing.
 */
export async function apiRequest<T>(
  path: string,
  schema: z.ZodType<T>,
  init: RequestInit = {},
): Promise<T> {
  const url = buildUrl(path);
  const headers: Record<string, string> = {
    Accept: "application/json",
    ...(init.body ? { "Content-Type": "application/json" } : {}),
    ...(init.headers as Record<string, string> | undefined),
  };
  const tenantId = getSelectedTenantId();
  if (tenantId) {
    headers[TENANT_HEADER] = tenantId;
  }
  let response: Response;
  try {
    response = await fetch(url, { ...init, headers, credentials: "include" });
  } catch {
    throw new ApiError(0, "network_error", `Could not reach the API at ${url}.`);
  }

  const requestId = response.headers.get("X-Request-ID") ?? undefined;
  const payload: unknown = await response.json().catch(() => null);

  if (!response.ok) {
    const parsed = apiErrorSchema.safeParse(payload);
    const error = parsed.data?.error;
    throw new ApiError(
      response.status,
      error?.code ?? "unknown_error",
      error?.message ?? `Request failed with status ${response.status}.`,
      error?.request_id ?? requestId,
    );
  }

  const result = schema.safeParse(payload);
  if (!result.success) {
    throw new ApiError(
      response.status,
      "invalid_response",
      "The API returned an unexpected response shape.",
      requestId,
    );
  }
  return result.data;
}
