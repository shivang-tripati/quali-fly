"use client";

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { ErrorState, InlineError } from "@/components/ui/states";
import { Skeleton } from "@/components/ui/skeleton";
import { useCurrentTenant, useTenants } from "@/lib/auth/hooks";
import { dashboardOptions } from "@/lib/api/dashboard";
import { startMetaOAuth } from "@/lib/api/meta";

/**
 * First-run onboarding.
 *
 * Deliberately one screen, not a wizard. The product is a SaaS that only does anything once
 * a Meta Page is connected, so the only step that maps onto real functionality is that one.
 * There is no second step here because there is no second real thing to do - inventing a
 * checklist with items that lead nowhere is worse than no checklist.
 *
 * **No onboarding state is stored.** Whether this workspace is new is read from the existing
 * `integration.meta_connected` on the dashboard payload: a workspace with no Meta connection
 * is a workspace that cannot receive leads, whatever its age. That means a schema migration,
 * a dismissal flag and a "first visit" heuristic all stay out of the codebase, and an
 * existing customer whose connection later drops sees the same honest prompt rather than a
 * page that thinks it has already been completed.
 *
 * **Nothing here claims a success that has not happened.** No fabricated lead counts, no
 * "you're all set" unless Meta is genuinely connected, and if the Meta app is not configured
 * on the server the panel says so instead of offering a button that 503s.
 */
export function WelcomeView() {
  const dashboard = useQuery(dashboardOptions());
  const { tenants } = useTenants();
  const { tenantId } = useCurrentTenant();
  // The name of the workspace the request layer is actually using, not the first one in the
  // list. A user in two workspaces would otherwise be greeted by name for one of them while
  // every request underneath went to the other.
  const workspaceName = tenants.find((t) => t.id === tenantId)?.name;

  if (dashboard.isPending) {
    return (
      <div className="flex flex-col gap-4">
        <Skeleton className="h-8 w-64" />
        <Skeleton className="h-40 w-full rounded-lg" />
      </div>
    );
  }

  // Only an owner or admin gets `integration` at all - a member is not shown a connection
  // prompt they cannot act on, and neither are they told the workspace is unconfigured.
  const integration = dashboard.data?.integration;
  if (dashboard.isError) {
    return (
      <ErrorState
        body="Could not check your workspace setup."
        onRetry={() => void dashboard.refetch()}
      />
    );
  }

  if (integration == null) {
    // A MEMBER. The dashboard omits integration state for anyone below ADMIN, so there is
    // nothing honest to tell them about connections, and offering a Connect button would be
    // offering an action the API would reject.
    return (
      <div className="flex flex-col gap-2">
        <h1 className="text-2xl font-semibold tracking-tight">Welcome to QualiFly</h1>
        <p className="text-sm text-muted-foreground">
          {workspaceName ?? "Your workspace"} is ready. Ask a workspace owner if you need
          access changed.
        </p>
        <Button asChild size="sm" className="mt-2 self-start">
          <a href="/dashboard">Go to the dashboard</a>
        </Button>
      </div>
    );
  }

  if (integration.meta_connected) {
    return (
      <div className="flex flex-col gap-2">
        <h1 className="text-2xl font-semibold tracking-tight">Welcome to QualiFly</h1>
        <p className="text-sm text-muted-foreground">
          {workspaceName ?? "Your workspace"} is connected to Meta and ready to receive
          leads. Nothing else to set up.
        </p>
        <Button asChild size="sm" className="mt-2 self-start">
          <a href="/dashboard">Go to the dashboard</a>
        </Button>
      </div>
    );
  }

  return <ConnectStep workspaceName={workspaceName ?? "Your workspace"} />;
}

function ConnectStep({ workspaceName }: { workspaceName: string }) {
  const oauth = useStartOAuth();

  return (
    <div className="flex max-w-xl flex-col gap-5">
      <div className="flex flex-col gap-1.5">
        <h1 className="text-2xl font-semibold tracking-tight">Welcome to QualiFly</h1>
        <p className="text-sm text-muted-foreground">
          {workspaceName} is ready. To start receiving leads, connect the Facebook Page or
          Instagram account your ads run on.
        </p>
      </div>

      <div className="flex flex-col gap-3 rounded-lg border p-4">
        <p className="text-sm font-medium">Next step</p>
        <p className="text-sm text-muted-foreground">
          Connect Meta. QualiFly listens for leads and messages on the accounts you connect,
          qualifies them and follows up on your behalf.
        </p>
        {oauth.error != null && <InlineError error={oauth.error} onRetry={oauth.reset} />}
        <div className="flex flex-wrap items-center gap-2">
          <Button size="sm" disabled={oauth.isPending} onClick={oauth.mutate}>
            {oauth.isPending ? "Opening Meta…" : "Connect Meta"}
          </Button>
          <a
            href="/dashboard"
            className="text-sm text-muted-foreground underline underline-offset-4 hover:text-foreground hover:no-underline"
          >
            I&apos;ll do this later
          </a>
        </div>
      </div>
    </div>
  );
}

/**
 * Start the real OAuth flow.
 *
 * It is a `fetch` to `/meta/oauth/start` and then a `location.assign` to the URL the server
 * hands back, because the state value is single-use and server-bound: minting the URL in the
 * browser would mean holding a credential in JavaScript. The failure this surfaces is a real
 * one - an unconfigured Meta app answers 503, and the panel then says the connection is
 * unavailable rather than pretending a dialog is on its way.
 *
 * Exported because the Settings page needs exactly this, and two implementations of "fetch
 * the URL then navigate" is how one of them ends up as a plain `<a href>` that renders raw
 * JSON to the user. `oauth/start` deliberately returns JSON rather than issuing a 302 so the
 * SPA controls the navigation, which means a hyperlink to it is a broken control by
 * construction - not a bug in the component that uses it.
 */
export function useStartOAuth() {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>(null);

  return {
    isPending: pending,
    error,
    reset: () => setError(null),
    mutate: async () => {
      setPending(true);
      setError(null);
      try {
        const { authorize_url } = await startMetaOAuth();
        window.location.assign(authorize_url);
      } catch (cause) {
        setError(cause);
        setPending(false);
      }
    },
  };
}
