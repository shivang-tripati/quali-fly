"use client";

import {
  queryOptions,
  useMutation,
  useQueryClient,
  type QueryClient,
} from "@tanstack/react-query";
import { z } from "zod";

import { apiRequest } from "@/lib/api/client";
import { setSelectedTenantId } from "@/lib/tenant/store";

export const roleSchema = z.enum(["OWNER", "ADMIN", "MEMBER"]);

const userSchema = z.object({
  id: z.string().uuid(),
  email: z.string().email(),
  name: z.string(),
  is_active: z.boolean(),
});

const tenantSummarySchema = z.object({
  id: z.string().uuid(),
  name: z.string(),
  slug: z.string(),
  role: roleSchema,
});

const tenantContextSchema = z.object({
  tenant: z.object({ id: z.string().uuid(), name: z.string(), slug: z.string() }),
  role: roleSchema,
});

const memberSchema = z.object({
  membership_id: z.string().uuid(),
  user_id: z.string().uuid(),
  email: z.string().email(),
  name: z.string(),
  role: roleSchema,
  is_active: z.boolean(),
});

/**
 * The honest combined state of a capability.
 *
 * `state` is what the UI branches on and `platform_available` is why a screen can explain
 * which half of the problem it is. Modelling "the deployment has no provider configured" and
 * "you switched this off" as one boolean is the mistake Phase 10-O.1 was created to fix: a
 * tenant who sees "AI is off" when the deployment is actually missing a key concludes they
 * broke something, and concludes the wrong thing.
 */
const controlPlaneStateSchema = z.object({
  state: z.enum(["enabled", "disabled", "unavailable"]),
  active: z.boolean(),
  tenant_enabled: z.boolean(),
  platform_available: z.boolean(),
  unavailable_reason: z.string().nullable(),
});

const aiConfigSchema = controlPlaneStateSchema;

const businessProfileSchema = z.object({
  workspace_name: z.string(),
  business_name: z.string().nullable(),
  business_description: z.string().nullable(),
  resolved_name: z.string(),
  has_description: z.boolean(),
});

const followUpStepSchema = z.object({
  step: z.number().int(),
  delay_hours: z.number().int(),
  template: z.string(),
  ai_enhance: z.boolean(),
});

const followUpConfigSchema = z.object({
  enabled: z.boolean(),
  state: controlPlaneStateSchema,
  steps: z.array(followUpStepSchema),
  is_default: z.boolean(),
  ghosted_after_days: z.number().int(),
});

const createdMemberSchema = z.object({
  user_id: z.string().uuid(),
  email: z.string().email(),
  name: z.string(),
  role: roleSchema,
  one_time_password: z.string(),
});

export type User = z.infer<typeof userSchema>;
export type TenantSummary = z.infer<typeof tenantSummarySchema>;
export type TenantContext = z.infer<typeof tenantContextSchema>;
export type TenantMember = z.infer<typeof memberSchema>;
export type ControlPlaneState = z.infer<typeof controlPlaneStateSchema>;
export type BusinessProfile = z.infer<typeof businessProfileSchema>;
export type FollowUpConfig = z.infer<typeof followUpConfigSchema>;
export type CreatedMember = z.infer<typeof createdMemberSchema>;

export const MIN_PASSWORD_LENGTH = 8;

const registrationSchema = z.object({
  user: userSchema,
  workspace: z.object({ id: z.string().uuid(), name: z.string(), slug: z.string() }),
  role: roleSchema,
});

export const getCurrentUser = () => apiRequest("/api/v1/auth/me", userSchema);
export const getTenants = () => apiRequest("/api/v1/tenants", z.array(tenantSummarySchema));
export const getTenantContext = () => apiRequest("/api/v1/tenant/context", tenantContextSchema);
export const getTenantMembers = () =>
  apiRequest("/api/v1/tenant/members", z.array(memberSchema));

/**
 * Sign in.
 *
 * The only thing that changes is the HttpOnly cookie, which `apiRequest` already sends via
 * `credentials: "include"` - the token itself never passes through JavaScript. So this
 * returns the same `User` the session already knows how to read, and the caller's only job
 * is to let the queries that were failing as "signed out" try again.
 *
 * **The session is occasionally not readable on the very next request.** Measured against
 * the running stack, roughly one sign-in in six answered `/auth/me` with 401 even though the
 * session row was already committed and visible to an independent database connection, so
 * this is a transient read failure rather than a transaction-ordering problem - see
 * `backend/tests/test_login_boundary.py`, which pins the commit *before* the response and
 * fails if that ever stops being true. What causes the occasional miss is not established;
 * retrying is the honest response to an unexplained transient, and `retry: false` on the
 * identity queries would otherwise cache the 401 as a hard answer and bounce the user back
 * to the sign-in form.
 *
 * `settleSession` therefore waits for the session to actually be readable before anything
 * else is invalidated. It is a resilience measure, not a substitute for a backend guarantee,
 * and it is deliberately cheap: it gives up after ~2s and reports the real error from the
 * POST, so a wrong password costs the same as it always did.
 */
async function settleSession(queryClient: QueryClient, attempts = 6): Promise<void> {
  for (let attempt = 0; attempt < attempts; attempt++) {
    try {
      await getCurrentUser();
      return;
    } catch {
      // Not readable yet, or the credentials were wrong - a wrong password will fail every
      // attempt, so this stays cheap and the caller reports the real error from the POST.
      await new Promise((resolve) => setTimeout(resolve, 150 * (attempt + 1)));
    }
  }
}

export function useLogin() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (credentials: { email: string; password: string }) =>
      apiRequest("/api/v1/auth/login", z.object({ user: userSchema }), {
        method: "POST",
        body: JSON.stringify(credentials),
      }),
    onSuccess: async () => {
      await settleSession(queryClient);
      // Everything the shell renders is behind these keys, and all of it was fetched while
      // signed out and cached as an error. Signing in has to retry them, not just the user.
      void queryClient.invalidateQueries();
    },
  });
}

/**
 * Create an account, its first workspace, and a session.
 *
 * Only these four fields are sent. The workspace id, the user id and the role are the
 * server's to decide, so the client has no way to ask for a different tenant or a
 * privileged role even by accident.
 *
 * The new workspace id is written straight into the tenant store rather than being left
 * for the shell's "adopt the first membership" effect. That effect would pick the right
 * one anyway - a new account has exactly one membership - but it only runs after
 * `/tenants` resolves, so without this the first tenant-scoped requests would go out with
 * no `X-Tenant-ID` and come back `TENANT_REQUIRED`. Setting it here is the same
 * preference header the switcher writes, so it grants nothing the backend would not
 * re-check; it just means the workspace is known on the first request.
 */
export function useRegister() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (details: {
      name: string;
      email: string;
      password: string;
      workspace_name: string;
    }) =>
      apiRequest("/api/v1/auth/register", registrationSchema, {
        method: "POST",
        body: JSON.stringify(details),
      }),
    onSuccess: async (registration) => {
      setSelectedTenantId(registration.workspace.id);
      // Same reason as sign-in: the session commit is durable before this response
      // (`backend/tests/test_registration.py`), but the read is still worth settling so a
      // transient miss does not bounce a brand-new customer back to the form.
      await settleSession(queryClient);
      void queryClient.invalidateQueries();
      return registration;
    },
  });
}

// -- Phase 10-O.1: the tenant control plane ------------------------------------
//
// One invalidation key per capability rather than invalidating everything on every save, so
// saving a business description does not re-fetch the follow-up sequence and the AI status.

const controlPlaneKeys = {
  profile: ["tenant", "profile"] as const,
  ai: ["tenant", "ai"] as const,
  followUps: ["tenant", "follow-ups"] as const,
  members: ["tenant", "members"] as const,
};

/**
 * The identity queries.
 *
 * `retry: false` on all of them is deliberate and load-bearing: these are the answers that
 * decide whether the shell renders the sign-in form or the product, so a transient 401
 * cached as a hard answer bounces a signed-in user out of the app. `settleSession` retries
 * by hand instead, where it can afford to.
 */
export const currentUserOptions = () =>
  queryOptions({ queryKey: ["auth", "me"], queryFn: getCurrentUser, retry: false });

export const tenantsOptions = () =>
  queryOptions({ queryKey: ["tenants"], queryFn: getTenants, retry: false });

export const tenantContextOptions = () =>
  queryOptions({ queryKey: ["tenant", "context"], queryFn: getTenantContext, retry: false });

export const businessProfileOptions = () =>
  queryOptions({
    queryKey: controlPlaneKeys.profile,
    queryFn: () => apiRequest("/api/v1/tenant/profile", businessProfileSchema),
    retry: false,
  });

export const aiConfigOptions = () =>
  queryOptions({
    queryKey: controlPlaneKeys.ai,
    queryFn: () => apiRequest("/api/v1/tenant/ai", aiConfigSchema),
    retry: false,
  });

export const followUpConfigOptions = () =>
  queryOptions({
    queryKey: controlPlaneKeys.followUps,
    queryFn: () => apiRequest("/api/v1/tenant/follow-ups", followUpConfigSchema),
    retry: false,
  });

export const tenantMembersOptions = () =>
  queryOptions({
    queryKey: ["tenant", "members"],
    queryFn: () => apiRequest("/api/v1/tenant/members", z.array(memberSchema)),
    retry: false,
  });

export function useSaveBusinessProfile() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (values: { business_name?: string | null; business_description?: string | null }) =>
      apiRequest("/api/v1/tenant/profile", businessProfileSchema, {
        method: "PUT",
        body: JSON.stringify(values),
      }),
    onSuccess: (profile) => {
      // The workspace switcher shows the tenant name, so it must follow a rename too.
      queryClient.setQueryData(controlPlaneKeys.profile, profile);
      void queryClient.invalidateQueries({ queryKey: ["tenants"] });
    },
  });
}

/**
 * Switch this tenant's AI on or off.
 *
 * The switch is the tenant's *preference*. The server reports `platform_available` and
 * `unavailable_reason` separately, and this mutation deliberately does not interpret them -
 * the screen does, in words, because "your deployment has no provider configured" and "you
 * turned AI off" need different explanations and different next steps.
 */
export function useSetAiEnabled() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (enabled: boolean) =>
      apiRequest("/api/v1/tenant/ai", aiConfigSchema, {
        method: "PUT",
        body: JSON.stringify({ enabled }),
      }),
    onSuccess: (state) => queryClient.setQueryData(controlPlaneKeys.ai, state),
  });
}

export function useSaveFollowUpConfig() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (values: {
      enabled?: boolean;
      steps?: { step: number; delay_hours: number; template: string; ai_enhance: boolean }[];
      reset?: boolean;
    }) =>
      apiRequest("/api/v1/tenant/follow-ups", followUpConfigSchema, {
        method: "PUT",
        body: JSON.stringify(values),
      }),
    onSuccess: (config) => queryClient.setQueryData(controlPlaneKeys.followUps, config),
  });
}

/**
 * Onboard somebody into this workspace.
 *
 * `one_time_password` is returned by the server and shown once. It is not stored anywhere by
 * the client, not put in localStorage, and not logged - the value lives in this mutation's
 * response for exactly as long as the success panel is on screen.
 */
export function useCreateTenantMember() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (values: { email: string; name: string; role: string }) =>
      apiRequest("/api/v1/tenant/members", createdMemberSchema, {
        method: "POST",
        body: JSON.stringify(values),
      }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: controlPlaneKeys.members }),
  });
}

/** Change the signed-in user's own password. Revokes every other session. */
export function useChangePassword() {
  return useMutation({
    mutationFn: (values: { current_password: string; new_password: string }) =>
      apiRequest(
        "/api/v1/auth/password",
        z.null(),
        { method: "POST", body: JSON.stringify(values) },
      ),
  });
}