"use client";

/**
 * Which tenant the user has selected.
 *
 * This is UI state only. The backend re-verifies the session and the membership on
 * every request, so writing an arbitrary id here grants nothing.
 *
 * Exposed as a `useSyncExternalStore` source so components never set state from an
 * effect, and so a change in one component updates every subscriber.
 */

const TENANT_STORAGE_KEY = "qualifly.selectedTenant";

export const TENANT_STORAGE = TENANT_STORAGE_KEY;

const listeners = new Set<() => void>();

export function getSelectedTenantId(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(TENANT_STORAGE_KEY);
}

/** Server rendering has no localStorage, so the snapshot must be null. */
export function getServerSelectedTenantId(): string | null {
  return null;
}

export function subscribeToSelectedTenant(listener: () => void): () => void {
  listeners.add(listener);
  window.addEventListener("storage", listener);
  return () => {
    listeners.delete(listener);
    window.removeEventListener("storage", listener);
  };
}

export function setSelectedTenantId(tenantId: string | null): void {
  if (typeof window === "undefined") return;
  if (tenantId === null) {
    window.localStorage.removeItem(TENANT_STORAGE_KEY);
  } else {
    window.localStorage.setItem(TENANT_STORAGE_KEY, tenantId);
  }
  for (const listener of listeners) listener();
}
