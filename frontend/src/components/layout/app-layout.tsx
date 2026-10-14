"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Menu, X } from "lucide-react";

import { NAV_ITEMS, SidebarFooterLink, SidebarNav, WorkspaceMark } from "@/components/layout/sidebar-nav";
import { CommandPaletteProvider, SearchTrigger } from "@/components/layout/command-palette";
import { NotificationBell } from "@/components/layout/notification-bell";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useCurrentTenant, useCurrentUser, useTenants } from "@/lib/auth/hooks";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useQuery } from "@tanstack/react-query";
import { dashboardOptions } from "@/lib/api/dashboard";

/**
 * The application shell: sidebar, topbar, and the content area.
 *
 * A client component, because the tenant switcher, the notification bell, the command
 * palette and the active-route highlight are all interactive. The pages inside it are still
 * free to be server components - only the frame is client-rendered, so the shell is one
 * hydration boundary rather than the whole tree.
 *
 * Four decisions worth stating:
 *
 * **Signed out is a redirect, not a screen.** This shell used to render the sign-in form in
 * place of `children`. That put a sidebar, a workspace switcher and a notification bell on
 * the sign-in screen - every one of which fetches tenant-scoped data, so a visitor trying to
 * create an account also paid for a burst of guaranteed 403s to render chrome they could
 * not use. The auth pages have their own layout under `(auth)`, and this one sends anyone
 * without a session to `/signin` instead.
 *
 * **The shell paints immediately and fills in.** It does not wait for the session to decide
 * whether to redirect, so the frame is already on screen while `/auth/me` is in flight. The
 * skeleton below is that state, and it is the same one used for a tenant being resolved.
 *
 * **Mobile is a drawer, not a squeezed sidebar.** A 240px sidebar on a 390px screen leaves
 * 150px of content. Below `lg` the sidebar becomes an overlay that closes on navigation.
 */
export function AppLayout({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const { user, isLoading, isAuthenticated } = useCurrentUser();
  const { tenants, isLoading: tenantsLoading } = useTenants();
  const { tenantId, selectTenant } = useCurrentTenant();

  // A signed-out visitor does not belong here. `replace` rather than `push` so the
  // protected URL they asked for is not left in history - hitting back would bounce them
  // straight to `/signin` again, which reads as a broken back button.
  //
  // Gated on `!isLoading` so this cannot fire during the first render, while the answer is
  // still unknown: redirecting on "not yet loaded" would sign out every real user on every
  // hard refresh. That race is why the skeleton covers the gap instead.
  useEffect(() => {
    if (!isLoading && !isAuthenticated) router.replace("/signin");
  }, [isLoading, isAuthenticated, router]);

  // The drawer closes from the link's own `onNavigate`, not from an effect watching the
  // pathname. Watching the route is the "derive state from a subscription" pattern: it closes
  // the drawer one frame *after* navigation, so the menu visibly covers the page the user
  // just asked for.
  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") setDrawerOpen(false);
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  // Adopt a workspace when the user has not chosen one yet.
  //
  // The store only ever held what a person explicitly picked, so on a first visit
  // `tenantId` was null while the switcher *displayed* `tenants[0]` as active. The request
  // layer reads the store, not the switcher, so every tenant-scoped request went out with no
  // `X-Tenant-ID` - and the API answers `TENANT_REQUIRED` for any user in more than one
  // workspace. The product was showing a selected workspace it was not actually using.
  //
  // Defaulting to the first membership is safe precisely because it grants nothing: the
  // backend re-checks the session and the membership, and this id is only ever a preference
  // header. It is also the same tenant the switcher was already showing, so nothing on
  // screen changes.
  useEffect(() => {
    if (tenants.length > 0 && !tenantId) selectTenant(tenants[0].id);
  }, [tenants, tenantId, selectTenant]);

  // A workspace the user belongs to, but has not been resolved into the store yet. The page
  // below is held back until it is, because every query inside it is tenant-scoped and would
  // otherwise fire once with no tenant header and be rejected. A single guaranteed-400 on
  // first paint is a real defect the user sees as an empty screen, not a transient retry.
  const needsTenant = isAuthenticated && tenants.length > 0 && !tenantId;

  // One aggregate read, used only for the sidebar badge. The dashboard page fetches the
  // same payload under the same key, so this is a cache hit rather than a second request.
  // Gated on the tenant, not just the session, for the reason above.
  const dashboard = useQuery({
    ...dashboardOptions(),
    enabled: isAuthenticated && Boolean(tenantId),
  });
  const waiting = dashboard.data?.summary.handoffs_unassigned ?? 0;

  const items = NAV_ITEMS.map((item) =>
    item.href === "/handoffs" && waiting > 0 ? { ...item, count: waiting } : item,
  );

  // `needsTenant` is folded into `loading` rather than given its own branch: from the
  // user's side, "we are working out which workspace this is" and "we are loading" are the
  // same screen, and splitting them would only add a frame of flash. A user with no
  // workspace at all still falls through to `children`, which is where the "ask an owner to
  // invite you" state belongs.
  const loading = isLoading || tenantsLoading || needsTenant;

  // Signed out: no shell at all, not even for one frame.
  //
  // The redirect above is already in flight by the time this renders, but returning the
  // chrome here would put a sidebar and a workspace switcher on screen during it - which is
  // the exact thing the `(auth)` split exists to prevent, and a visitor deep-linking to
  // `/leads` would see it. A bare skeleton is the honest rendering of "we are sending you
  // somewhere else", and it is the same surface the loading state uses.
  if (!isLoading && !isAuthenticated) {
    return <ShellSkeleton />;
  }

  return (
    <CommandPaletteProvider open={paletteOpen} onOpenChange={setPaletteOpen}>
      <div className="flex min-h-dvh flex-col bg-background">
        <header className="sticky top-0 z-30 flex h-14 items-center gap-2 border-b bg-background/95 px-3 backdrop-blur lg:px-4">
          <Button
            variant="ghost"
            size="icon"
            className="lg:hidden"
            aria-label={drawerOpen ? "Close navigation" : "Open navigation"}
            aria-expanded={drawerOpen}
            onClick={() => setDrawerOpen((o) => !o)}
          >
            <Menu className="size-4" />
          </Button>

          <a href="/dashboard" className="flex items-center gap-2 font-semibold">
            QualiFly
          </a>

          <div className="ml-auto flex items-center gap-1.5">
            <SearchTrigger onClick={() => setPaletteOpen(true)} />
            {/* Gated on the resolved tenant for the same reason as the dashboard query
                below. The bell fetches on mount, so leaving it ungated meant it fired while
                the tenant was still null and drew a guaranteed TENANT_REQUIRED - the last
                one in the shell, and the only one a user could still see as an error. */}
            {isAuthenticated && Boolean(tenantId) && <NotificationBell />}
          </div>
        </header>
        <div className="flex min-h-0 flex-1">
          {/* Desktop sidebar */}
          <aside className="hidden w-60 shrink-0 flex-col gap-4 border-r px-3 py-4 lg:flex">
            <WorkspaceSwitcher
              loading={loading}
              tenants={tenants}
              activeTenantId={tenantId ?? tenants[0]?.id ?? null}
              onSelect={selectTenant}
            />
            <SidebarNav items={items} />
            <div className="mt-auto flex flex-col gap-1">
              <SidebarFooterLink />
              {user && (
                <p className="truncate px-3 py-1 text-xs text-muted-foreground">{user.email}</p>
              )}
            </div>
          </aside>

          {/* Mobile drawer */}
          {drawerOpen && (
            <>
              <button
                type="button"
                aria-label="Close navigation"
                onClick={() => setDrawerOpen(false)}
                className="fixed inset-0 z-40 bg-black/40 lg:hidden"
              />
              <aside className="fixed inset-y-0 left-0 z-50 flex w-64 flex-col gap-4 border-r bg-background px-3 py-4 lg:hidden">
                <div className="flex items-center justify-between">
                  <span className="font-semibold">Menu</span>
                  <Button variant="ghost" size="icon" onClick={() => setDrawerOpen(false)}>
                    <X aria-hidden className="size-4" />
                    <span className="sr-only">Close navigation</span>
                  </Button>
                </div>
                <WorkspaceSwitcher
                  loading={loading}
                  tenants={tenants}
                  activeTenantId={tenantId ?? tenants[0]?.id ?? null}
                  onSelect={selectTenant}
                />
                <SidebarNav items={items} onNavigate={() => setDrawerOpen(false)} />
                <div className="mt-auto">
                  <SidebarFooterLink onNavigate={() => setDrawerOpen(false)} />
                </div>
              </aside>
            </>
          )}

          <main className="min-w-0 flex-1">
            {loading ? <ShellSkeleton /> : children}
          </main>
        </div>
      </div>
    </CommandPaletteProvider>
  );
}

/**
 * The frame with no frame around it.
 *
 * Used for both "we are still finding out whether there is a session" and "there is no
 * session, we are redirecting". One component for both so the two states cannot drift into
 * looking like different products.
 */
function ShellSkeleton() {
  return (
    <div className="min-h-dvh bg-background">
      <div className="mx-auto w-full max-w-6xl px-4 py-6 lg:px-6">
        <Skeleton className="h-8 w-52" />
        <Skeleton className="mt-4 h-24 w-full rounded-lg" />
        <Skeleton className="mt-4 h-40 w-full rounded-lg" />
      </div>
    </div>
  );
}

function WorkspaceSwitcher({
  tenants,
  activeTenantId,
  onSelect,
  loading,
}: {
  tenants: { id: string; name: string; slug: string; role: string }[];
  activeTenantId: string | null;
  onSelect: (id: string) => void;
  loading: boolean;
}) {
  if (loading) {
    return <Skeleton className="h-9 w-full rounded-lg" />;
  }
  if (tenants.length === 0) {
    return (
      <p className="px-3 text-xs text-muted-foreground">
        No workspace yet. Ask an owner to invite you.
      </p>
    );
  }
  const active = tenants.find((t) => t.id === activeTenantId) ?? tenants[0];
  if (tenants.length === 1) {
    return (
      <div className="flex items-center gap-2 px-3 py-1.5">
        <WorkspaceMark name={active.name} />
        <span className="min-w-0 flex-1">
          <span className="block truncate text-sm font-medium">{active.name}</span>
        </span>
      </div>
    );
  }
  return (
    <div className="px-1">
      <label htmlFor="workspace-switcher" className="sr-only">
        Workspace
      </label>
      <Select value={active.id} onValueChange={onSelect}>
        <SelectTrigger id="workspace-switcher" className="w-full">
          <span className="flex items-center gap-2 truncate">
            <WorkspaceMark name={active.name} />
            <SelectValue placeholder="Choose a workspace" />
          </span>
        </SelectTrigger>
        <SelectContent>
          {tenants.map((tenant) => (
            <SelectItem key={tenant.id} value={tenant.id}>
              {tenant.name} · {tenant.role.toLowerCase()}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}


