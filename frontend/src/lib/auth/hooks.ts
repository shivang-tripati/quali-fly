"use client";

import { useCallback, useSyncExternalStore } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import {
  type TenantSummary,
  type User,
  currentUserOptions,
  tenantsOptions,
} from "@/lib/api/identity";
import {
  getSelectedTenantId,
  getServerSelectedTenantId,
  setSelectedTenantId,
  subscribeToSelectedTenant,
} from "@/lib/tenant/store";

/** The authenticated user, or `null` when the session is missing or rejected. */
export function useCurrentUser() {
  const query = useQuery(currentUserOptions());
  return {
    user: query.data ?? null,
    isLoading: query.isPending,
    isAuthenticated: Boolean(query.data),
  };
}

/** Tenants the user belongs to. Enforced server-side; listed here for the UI. */
export function useTenants() {
  const query = useQuery(tenantsOptions());
  return { tenants: query.data ?? [], isLoading: query.isPending };
}

/** The currently selected tenant id, persisted in localStorage. */
export function useCurrentTenant() {
  const tenantId = useSyncExternalStore(
    subscribeToSelectedTenant,
    getSelectedTenantId,
    getServerSelectedTenantId,
  );
  const selectTenant = useCallback((next: string | null) => setSelectedTenantId(next), []);
  return { tenantId, selectTenant };
}

/** Drop every cached tenant query, e.g. after switching tenant or logging out. */
export function useResetIdentity() {
  const queryClient = useQueryClient();
  return useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ["tenant"] });
    void queryClient.invalidateQueries({ queryKey: ["tenants"] });
  }, [queryClient]);
}

export function tenantLabel(tenant: Pick<TenantSummary, "name" | "slug">): string {
  return `${tenant.name} (${tenant.slug})`;
}

export function userLabel(user: Pick<User, "name" | "email">): string {
  return `${user.name} · ${user.email}`;
}
