"use client";

import { useState } from "react";
import { useSearchParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { CheckCircle2, Plug } from "lucide-react";

import { Button } from "@/components/ui/button";
import { EmptyState, ErrorState, InlineError } from "@/components/ui/states";
import { Skeleton } from "@/components/ui/skeleton";
import { StatusBadge } from "@/components/ui/status-badge";
import { Switch } from "@/components/ui/switch";
import { useStartOAuth } from "@/components/auth/welcome-view";
import {
  instagramAccountsOptions,
  metaConnectionsOptions,
  useDisconnectMeta,
  useSelectInstagramAccount,
  useSelectWhatsAppBusinessAccount,
  useSelectWhatsAppNumber,
  whatsappNumbersOptions,
  whatsappSetupOptions,
} from "@/lib/api/meta";
import {
  aiConfigOptions,
  businessProfileOptions,
  followUpConfigOptions,
  tenantContextOptions,
  tenantMembersOptions,
  useCreateTenantMember,
  useSaveBusinessProfile,
  useSaveFollowUpConfig,
  useSetAiEnabled,
  type CreatedMember,
} from "@/lib/api/identity";
import { absoluteTime, relativeTime } from "@/lib/format";

type FollowUpStep = {
  step: number;
  delay_hours: number;
  template: string;
  ai_enhance: boolean;
};

/**
 * Settings.
 *
 * Only what the backend actually supports. There is no "AI model" dropdown and no
 * "notification preferences" section, because there is no endpoint behind either - a settings
 * page of dead controls teaches users that the product's configuration is decorative.
 *
 * Phase 10-O.1 added the four panels a tenant previously had no way to reach at all: its
 * business identity (which the AI is told about), its own AI and follow-up switches, its
 * follow-up sequence, and team onboarding. Every one of them is backed by a real endpoint;
 * nothing here is optimistic UI over a column nothing reads.
 *
 * The rule that survived all of this: a control is shown only if the server can enforce it.
 * Where the *deployment* rather than the tenant is the thing that is wrong - no AI provider
 * configured, follow-ups disabled platform-wide - the panel says so in words instead of
 * showing a switch that appears to work.
 *
 * The Meta connection is the one that earns prominence: if it is broken, nothing arrives at
 * all, and every other number on the dashboard is stale without saying so. So its state is
 * the first thing on the page and its failure mode is a direct CTA.
 */
type Tab = "workspace" | "ai" | "follow-ups" | "members" | "meta";

const TABS: { value: Tab; label: string }[] = [
  { value: "workspace", label: "Workspace" },
  { value: "ai", label: "AI" },
  { value: "follow-ups", label: "Follow-ups" },
  { value: "members", label: "Members" },
  { value: "meta", label: "Meta connection" },
];

export function SettingsView() {
  const params = useSearchParams();
  const initial = (params.get("tab") as Tab | null) ?? "workspace";
  const [tab, setTab] = useState<Tab>(TABS.some((t) => t.value === initial) ? initial : "workspace");

  return (
    <div className="mx-auto w-full max-w-3xl px-4 py-6 lg:px-6 lg:py-8">
      <header className="flex flex-col gap-1">
        <h1 className="text-2xl font-semibold tracking-tight">Settings</h1>
        <p className="text-sm text-muted-foreground">
          Everything here is something your team can actually change.
        </p>
      </header>

      <div className="mt-5 flex gap-1.5" role="tablist" aria-label="Settings sections">
        {TABS.map((option) => (
          <button
            key={option.value}
            role="tab"
            type="button"
            aria-selected={tab === option.value}
            onClick={() => setTab(option.value)}
            className={
              tab === option.value
                ? "rounded-full border px-3 py-1 text-xs font-medium"
                : "rounded-full border border-muted px-3 py-1 text-xs text-muted-foreground hover:bg-accent"
            }
          >
            {option.label}
          </button>
        ))}
      </div>

      <div className="mt-5">
        {tab === "workspace" && <WorkspacePanel />}
        {tab === "ai" && <AiPanel />}
        {tab === "follow-ups" && <FollowUpPanel />}
        {tab === "members" && <MembersPanel />}
        {tab === "meta" && <MetaPanel />}
      </div>
    </div>
  );
}

function WorkspacePanel() {
  const context = useQuery(tenantContextOptions());
  const profile = useQuery(businessProfileOptions());
  const save = useSaveBusinessProfile();
  const [name, setName] = useState<string | null>(null);
  const [description, setDescription] = useState<string | null>(null);

  if (context.isPending || profile.isPending) {
    return <Skeleton className="h-32 w-full rounded-lg" />;
  }
  if (context.isError) {
    return (
      <ErrorState
        body="Could not load your workspace details."
        onRetry={() => void context.refetch()}
      />
    );
  }
  if (profile.isError) {
    return (
      <ErrorState
        body="Could not load your business details."
        onRetry={() => void profile.refetch()}
      />
    );
  }

  const data = profile.data;
  // `null` means "not edited yet", so the inputs start from the server's value and only
  // diverge once someone types. Initialising from state would otherwise need an effect to
  // resync after a save, which is the "derive state from a subscription" mistake.
  const nameValue = name ?? data.business_name ?? "";
  const descriptionValue = description ?? data.business_description ?? "";
  const dirty = nameValue !== (data.business_name ?? "") ||
    descriptionValue !== (data.business_description ?? "");

  return (
    <div className="flex flex-col gap-6">
      <dl className="divide-y overflow-hidden rounded-lg border">
        <Row label="Workspace name" value={context.data.tenant.name} />
        <Row
          label="Identifier"
          value={<span className="font-mono text-xs">{context.data.tenant.slug}</span>}
        />
        <Row
          label="Your role"
          value={context.data.role.toLowerCase()}
          hint={
            context.data.role === "MEMBER"
              ? "Members can work leads, conversations and handoffs, but cannot change workspace settings."
              : "Admins and owners can also change workspace settings and onboard teammates."
          }
        />
      </dl>

      <form
        className="flex flex-col gap-3"
        onSubmit={(event) => {
          event.preventDefault();
          save.mutate({
            business_name: nameValue.trim() || null,
            business_description: descriptionValue.trim() || null,
          });
        }}
      >
        <div className="flex flex-col gap-1.5">
          <label htmlFor="business-name" className="text-sm font-medium">
            Business name
          </label>
          <input
            id="business-name"
            type="text"
            value={nameValue}
            onChange={(event) => setName(event.target.value)}
            placeholder={context.data.tenant.name}
            className="rounded-md border bg-background px-3 py-2 text-sm focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
          />
          <p className="text-xs text-muted-foreground">
            What you would say out loud to a customer. The AI uses{" "}
            <span className="font-medium">{data.resolved_name}</span> when it replies.
          </p>
        </div>

        <div className="flex flex-col gap-1.5">
          <label htmlFor="business-description" className="text-sm font-medium">
            What your business does
          </label>
          <textarea
            id="business-description"
            rows={5}
            value={descriptionValue}
            onChange={(event) => setDescription(event.target.value)}
            placeholder="What you sell, who you sell it to, opening hours, service area, anything the AI should not have to guess at."
            className="rounded-md border bg-background px-3 py-2 text-sm focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
          />
          <p className="text-xs text-muted-foreground">
            This is the single biggest thing you can do to improve reply quality. Without it
            the AI is told it knows nothing about your business and asks the customer to
            describe what they need.
          </p>
        </div>

        {save.isError && <InlineError error={save.error} onRetry={() => save.reset()} />}
        {save.isSuccess && !dirty && <SavedNote />}
        <div>
          <Button type="submit" size="sm" disabled={!dirty || save.isPending}>
            {save.isPending ? "Saving…" : "Save business details"}
          </Button>
        </div>
      </form>
    </div>
  );
}

function SavedNote() {
  return (
    <p className="text-xs text-emerald-700 dark:text-emerald-400" role="status">
      Saved. Your AI will use this from the next conversation.
    </p>
  );
}

/**
 * The AI switch, and the honest reason it may not do anything.
 *
 * A tenant's own preference and the deployment's capability are shown separately, because
 * conflating them is the failure this phase exists to fix: a switch that appears to be on
 * while the platform has no provider key teaches a tenant that the product is broken, when
 * in fact the deployment is unconfigured. When the platform cannot run AI, the switch is
 * disabled and the reason is stated.
 */
function AiPanel() {
  const config = useQuery(aiConfigOptions());
  const setEnabled = useSetAiEnabled();

  if (config.isPending) return <Skeleton className="h-40 w-full rounded-lg" />;
  if (config.isError) {
    return (
      <ErrorState
        body="Could not load your AI settings."
        onRetry={() => void config.refetch()}
      />
    );
  }

  const data = config.data;
  const unavailable = !data.platform_available;
  const reason =
    data.unavailable_reason === "no_ai_provider_configured"
      ? "This QualiFly installation has no AI provider configured, so no workspace can use AI yet. Your switch is remembered and will take effect as soon as one is set up."
      : "AI is switched off for this whole installation by whoever runs it. Your switch is remembered.";

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-start justify-between gap-4 rounded-lg border p-4">
        <div className="flex flex-col gap-1">
          <span className="text-sm font-medium">AI replies</span>
          <span className="text-xs text-muted-foreground">
            {data.state === "enabled"
              ? "Incoming messages are read, qualified, and answered automatically."
              : data.state === "disabled"
                ? "Messages are still stored and your team can reply, but nothing is answered automatically."
                : "Messages are still stored and your team can reply, but nothing is answered automatically right now."}
          </span>
        </div>
        <Switch
          checked={data.tenant_enabled}
          disabled={unavailable || setEnabled.isPending}
          label="AI replies"
          onChange={(next) => setEnabled.mutate(next)}
        />
      </div>

      <div className="flex items-center gap-2">
        <StatusBadge tone={data.active ? "success" : "neutral"}>
          {data.state === "enabled"
            ? "Active"
            : data.state === "disabled"
              ? "Off for your workspace"
              : "Unavailable on this installation"}
        </StatusBadge>
        {data.active && (
          <span className="text-xs text-muted-foreground">
            Qualification, replies and follow-up writing are all running.
          </span>
        )}
      </div>

      {unavailable && (
        <p className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-900 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-200">
          {reason}
        </p>
      )}

      {setEnabled.isError && (
        <InlineError error={setEnabled.error} onRetry={() => setEnabled.reset()} />
      )}

      <p className="text-xs text-muted-foreground">
        The AI model, temperature and escalation settings belong to whoever runs this
        installation, so they are not shown here. Your team cannot change them, and this page
        does not pretend otherwise.
      </p>
    </div>
  );
}

/**
 * The follow-up sequence.
 *
 * A real editor over the document the scheduler already reads, not a display of it. Steps
 * are edited as a set and saved together, because the engine only ever loads the whole
 * sequence - there is no partial-sequence state it could be half-updated into.
 *
 * The engine invariant this must not break: step N+1 is armed only after step N has actually
 * sent. That is why the UI is a flat list of steps rather than anything that implies a
 * pipeline runs on its own, and why no control here touches scheduling semantics.
 */
function FollowUpPanel() {
  const config = useQuery(followUpConfigOptions());
  const save = useSaveFollowUpConfig();
  const [steps, setSteps] = useState<FollowUpStep[] | null>(null);
  const [enabled, setEnabled] = useState<boolean | null>(null);

  if (config.isPending) return <Skeleton className="h-48 w-full rounded-lg" />;
  if (config.isError) {
    return (
      <ErrorState
        body="Could not load your follow-up settings."
        onRetry={() => void config.refetch()}
      />
    );
  }

  const data = config.data;
  const working = steps ?? data.steps;
  const enabledValue = enabled ?? data.enabled;
  const dirty =
    steps !== null || enabledValue !== data.enabled;

  const update = (index: number, patch: Partial<FollowUpStep>) =>
    setSteps(
      working.map((step, position) =>
        position === index ? { ...step, ...patch } : step,
      ),
    );

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-start justify-between gap-4 rounded-lg border p-4">
        <div className="flex flex-col gap-1">
          <span className="text-sm font-medium">Automated follow-ups</span>
          <span className="text-xs text-muted-foreground">
            When a conversation goes quiet, send a short message after a delay. Anyone who
            replies stops the sequence immediately.
          </span>
        </div>
        <Switch
          checked={enabledValue}
          disabled={save.isPending}
          label="Automated follow-ups"
          onChange={(next) => {
            setEnabled(next);
            save.mutate({ enabled: next });
          }}
        />
      </div>

      <div className="flex items-center gap-2">
        <StatusBadge tone={data.state.state === "enabled" ? "success" : "neutral"}>
          {data.state.state === "enabled"
            ? "Active"
            : data.state.state === "disabled"
              ? "Off for your workspace"
              : "Unavailable on this installation"}
        </StatusBadge>
        <span className="text-xs text-muted-foreground">
          {data.is_default
            ? "Using the built-in sequence."
            : "Customised by your team."}{" "}
          A conversation with no reply for {data.ghosted_after_days} days is marked ghosted.
        </span>
      </div>

      {!data.state.platform_available && (
        <p className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-900 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-200">
          Follow-ups are switched off for this whole installation by whoever runs it. Your
          settings are saved and will take effect when they are enabled.
        </p>
      )}

      <ol className="flex flex-col gap-3">
        {working.map((step, index) => (
          <li key={step.step} className="flex flex-col gap-2 rounded-lg border p-3">
            <div className="flex items-center gap-3">
              <span className="text-sm font-medium">Step {step.step}</span>
              <label className="ml-auto flex items-center gap-2 text-xs text-muted-foreground">
                Wait
                <input
                  type="number"
                  aria-label={`Step ${step.step} delay in hours`}
                  min={1}
                  max={720}
                  value={step.delay_hours}
                  onChange={(event) =>
                    update(index, {
                      delay_hours: Number(event.target.value) || 1,
                    })
                  }
                  className="w-16 rounded-md border bg-background px-2 py-1 text-sm focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
                />
                hours
              </label>
              <label className="flex items-center gap-2 text-xs text-muted-foreground">
                <input
                  type="checkbox"
                  checked={step.ai_enhance}
                  onChange={(event) => update(index, { ai_enhance: event.target.checked })}
                />
                Personalise with AI
              </label>
            </div>
            <textarea
              rows={2}
              value={step.template}
              aria-label={`Step ${step.step} message`}
              onChange={(event) => update(index, { template: event.target.value })}
              className="rounded-md border bg-background px-3 py-2 text-sm focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
            />
          </li>
        ))}
      </ol>

      <p className="text-xs text-muted-foreground">
        Use <span className="font-mono">{"{first_name}"}</span> for the customer&apos;s
        first name. Steps run in order, and each one is only scheduled after the previous
        one has actually been sent.
      </p>

      {save.isError && <InlineError error={save.error} onRetry={() => save.reset()} />}
      <div className="flex items-center gap-2">
        <Button
          size="sm"
          disabled={!dirty || save.isPending}
          onClick={() =>
            save.mutate(
              { steps: working },
              {
                onSuccess: () => {
                  setSteps(null);
                  setEnabled(null);
                },
              },
            )
          }
        >
          {save.isPending ? "Saving…" : "Save sequence"}
        </Button>
        {dirty && (
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              setSteps(null);
              setEnabled(null);
              save.reset();
            }}
          >
            Discard changes
          </Button>
        )}
        {!data.is_default && (
          <Button
            size="sm"
            variant="ghost"
            disabled={save.isPending}
            // A write, not a local edit: the built-in sequence lives on the server, so
            // pretending otherwise here would leave the tenant's own templates live.
            onClick={() =>
              save.mutate(
                { reset: true },
                { onSuccess: () => setSteps(null) },
              )
            }
          >
            Restore built-in sequence
          </Button>
        )}
      </div>
    </div>
  );
}

/**
 * The team, and onboarding somebody into it.
 *
 * `one_time_password` is shown exactly once, immediately after creation, and is not kept in
 * state that survives a reload - there is no mail delivery in this product to send an
 * invitation with, so the admin passes it on out of band and the new member changes it from
 * their own settings. Stated here as well, because a one-time secret that silently vanished
 * on refresh would leave an admin stuck with an account they cannot hand over.
 */
function MembersPanel() {
  const members = useQuery(tenantMembersOptions());
  const create = useCreateTenantMember();
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [role, setRole] = useState("MEMBER");
  const [issued, setIssued] = useState<CreatedMember | null>(null);

  if (members.isPending) return <Skeleton className="h-40 w-full rounded-lg" />;
  if (members.isError) {
    return (
      <ErrorState body="Could not load your team." onRetry={() => void members.refetch()} />
    );
  }

  return (
    <div className="flex flex-col gap-6">
      {issued && (
        <div className="flex flex-col gap-2 rounded-lg border border-emerald-200 bg-emerald-50 p-4 dark:border-emerald-900 dark:bg-emerald-950/30">
          <p className="text-sm font-medium text-emerald-900 dark:text-emerald-200">
            {issued.name} can now sign in
          </p>
          <p className="text-xs text-emerald-900/80 dark:text-emerald-200/80">
            Send them this one-time password. It is shown once and cannot be retrieved again,
            so they will need it to sign in the first time.
          </p>
          <code className="select-all rounded border bg-background px-2 py-1 font-mono text-xs">
            {issued.one_time_password}
          </code>
          <div>
            <Button size="xs" variant="outline" onClick={() => setIssued(null)}>
              I have sent it
            </Button>
          </div>
        </div>
      )}

      <ul className="divide-y overflow-hidden rounded-lg border">
        {members.data.map((member) => (
          <li
            key={member.membership_id}
            className="flex flex-wrap items-center gap-3 px-4 py-3"
          >
            <span className="flex min-w-0 flex-1 flex-col">
              <span className="truncate text-sm font-medium">{member.name}</span>
              <span className="truncate text-xs text-muted-foreground">{member.email}</span>
            </span>
            <StatusBadge tone="neutral">{member.role.toLowerCase()}</StatusBadge>
            {!member.is_active && <StatusBadge tone="neutral">inactive</StatusBadge>}
          </li>
        ))}
      </ul>

      <form
        className="flex flex-col gap-3 rounded-lg border p-4"
        onSubmit={(event) => {
          event.preventDefault();
          create.mutate(
            { email: email.trim(), name: name.trim(), role },
            {
              onSuccess: (created) => {
                setIssued(created);
                setEmail("");
                setName("");
              },
            },
          );
        }}
      >
        <span className="text-sm font-medium">Add a teammate</span>
        <div className="flex flex-col gap-3 sm:flex-row">
          <input
            type="text"
            aria-label="Name"
            required
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="Name"
            className="rounded-md border bg-background px-3 py-2 text-sm focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
          />
          <input
            type="email"
            aria-label="Email"
            required
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            placeholder="Email"
            className="rounded-md border bg-background px-3 py-2 text-sm focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
          />
          <select
            aria-label="Role"
            value={role}
            onChange={(event) => setRole(event.target.value)}
            className="rounded-md border bg-background px-3 py-2 text-sm focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
          >
            <option value="MEMBER">Member</option>
            <option value="ADMIN">Admin</option>
            <option value="OWNER">Owner</option>
          </select>
        </div>
        <p className="text-xs text-muted-foreground">
          Members can work leads, conversations and handoffs. Admins and owners can also
          change these settings. We generate a one-time password and show it once - there is
          no email sending in QualiFly yet, so you will need to pass it on yourself.
        </p>
        {create.isError && <InlineError error={create.error} onRetry={() => create.reset()} />}
        <div>
          <Button type="submit" size="sm" disabled={create.isPending}>
            {create.isPending ? "Adding…" : "Add teammate"}
          </Button>
        </div>
      </form>
    </div>
  );
}

/**
 * The Meta connection panel.
 *
 * **Connect and Reconnect are buttons, not links.** `GET /api/v1/meta/oauth/start`
 * deliberately returns `{"authorize_url": ...}` as JSON rather than issuing a 302, so the SPA
 * controls the navigation - which means an `<a href>` to it lands the admin on a raw JSON
 * blob and never reaches the Meta dialog. Both controls call the same `useStartOAuth` hook
 * the welcome screen uses, so there is exactly one OAuth implementation.
 *
 * **Disconnect is here because the endpoint existed with no way to reach it.**
 * `useDisconnectMeta` was defined in `lib/api/meta.ts` and called from nowhere.
 *
 * Still read-only about the credential: the panel reports whether a token is configured and
 * never shows one. Channel *readiness* - whether the webhook is subscribed, whether
 * messages are arriving, whether AI can actually run - is deliberately not claimed here; that
 * is the later readiness phase, and this panel says "Connected", not "Ready".
 */
function InstagramSection({ connectionId }: { connectionId: string }) {
  const accounts = useQuery(instagramAccountsOptions(connectionId));
  const select = useSelectInstagramAccount(connectionId);
  const [chosen, setChosen] = useState("");

  if (accounts.isPending) return <Skeleton className="h-20 w-full rounded-lg" />;

  if (accounts.isError) {
    return (
      <div className="flex flex-col gap-2 rounded-lg border border-dashed p-3">
        <span className="text-xs font-medium">Instagram</span>
        <p className="text-xs text-muted-foreground">
          Could not ask Meta which Instagram accounts this Page has.
        </p>
        <InlineError error={accounts.error} onRetry={() => void accounts.refetch()} />
      </div>
    );
  }

  const { accounts: options, connected_id: connected } = accounts.data;
  // What the select shows, and what a submit would send. Compared against `connected` so
  // the button is disabled when there is genuinely nothing to change - comparing the raw
  // `chosen` state would leave it enabled on the account already saved.
  const effective = chosen || connected || "";

  if (options.length === 0) {
    return (
      <div className="flex flex-col gap-1 rounded-lg border border-dashed p-3">
        <span className="text-xs font-medium">Instagram</span>
        <p className="text-xs text-muted-foreground">
          Meta reports no Instagram Business Account on this Facebook Page. Link one to
          the Page in Meta, then come back here.
        </p>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-2 rounded-lg border border-dashed p-3">
      <span className="text-xs font-medium">Instagram</span>
      {connected ? (
        <p className="text-xs text-muted-foreground">
          Operating{" "}
          <span className="font-mono text-foreground">
            {options.find((a) => a.instagram_business_id === connected)?.label ?? connected}
          </span>
          . Choose a different account to switch.
        </p>
      ) : (
        <p className="text-xs text-muted-foreground">
          Choose which Instagram Business account to receive messages on.
        </p>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <select
          aria-label="Instagram account"
          className="min-w-0 flex-1 rounded-md border bg-background px-2 py-1.5 text-sm focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
          value={effective}
          onChange={(event) => setChosen(event.target.value)}
        >
          {options.map((option) => (
            <option key={option.instagram_business_id} value={option.instagram_business_id}>
              {option.label}
              {option.username ? ` · @${option.username}` : ""}
            </option>
          ))}
        </select>
        <Button
          size="sm"
          disabled={select.isPending || !effective || effective === connected}
          onClick={() => select.mutate(effective)}
        >
          {select.isPending ? "Saving…" : connected ? "Switch account" : "Connect Instagram"}
        </Button>
      </div>
      {select.isError && <InlineError error={select.error} onRetry={() => select.reset()} />}
      {select.isSuccess && (
        <p className="text-xs text-muted-foreground">Instagram account saved.</p>
      )}
    </div>
  );
}

function WhatsAppSection() {
  const setup = useQuery(whatsappSetupOptions());
  const numbers = useQuery(whatsappNumbersOptions(setup.data?.meta_connection_id ?? null));
  const selectAccount = useSelectWhatsAppBusinessAccount(setup.data?.meta_connection_id ?? "");
  const selectNumber = useSelectWhatsAppNumber(setup.data?.meta_connection_id ?? "");
  const [accountId, setAccountId] = useState("");
  const [chosen, setChosen] = useState("");

  if (setup.isPending) return <Skeleton className="h-20 w-full rounded-lg" />;
  if (setup.isError) {
    return (
      <div className="flex flex-col gap-2 rounded-lg border border-dashed p-3">
        <span className="text-xs font-medium">WhatsApp</span>
        <InlineError error={setup.error} onRetry={() => void setup.refetch()} />
      </div>
    );
  }

  const state = setup.data;

  if (state.state === "not_connected") {
    return (
      <div className="flex flex-col gap-1 rounded-lg border border-dashed p-3">
        <span className="text-xs font-medium">WhatsApp</span>
        <p className="text-xs text-muted-foreground">
          Not set up. Connect Meta first, then add your WhatsApp Business Account here to
          choose the number messages are sent from.
        </p>
      </div>
    );
  }

  // No account bound yet: the next step is the account id, so ask for that and stop. The
  // number list cannot be fetched without it, and offering an empty list would look broken.
  if (state.state === "awaiting_account") {
    return (
      <div className="flex flex-col gap-2 rounded-lg border border-dashed p-3">
        <div className="flex items-center gap-2">
          <span className="text-xs font-medium">WhatsApp</span>
          <StatusBadge tone="warning">Needs your account</StatusBadge>
        </div>
        <p className="text-xs text-muted-foreground">
          Enter the WhatsApp Business Account ID from Meta. We check it with Meta before
          saving.
        </p>
        <div className="flex flex-wrap items-center gap-2">
          <input
            aria-label="WhatsApp Business Account ID"
            className="min-w-0 flex-1 rounded-md border bg-background px-2 py-1.5 text-sm focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
            inputMode="numeric"
            placeholder="e.g. 123456789012345"
            value={accountId}
            onChange={(event) => setAccountId(event.target.value.trim())}
          />
          <Button
            size="sm"
            disabled={selectAccount.isPending || accountId.length === 0}
            onClick={() => selectAccount.mutate(accountId)}
          >
            {selectAccount.isPending ? "Checking…" : "Add account"}
          </Button>
        </div>
        {selectAccount.isError && (
          <InlineError error={selectAccount.error} onRetry={() => selectAccount.reset()} />
        )}
      </div>
    );
  }

  const options = numbers.data?.numbers ?? [];
  // Falls back to the first discovered number so a fresh connection is one click, not a
  // select-then-click. Without it the button is disabled with nothing chosen, which reads
  // as "the control is broken" rather than "pick one".
  const effective =
    chosen || state.phone_number_id || options[0]?.phone_number_id || "";

  return (
    <div className="flex flex-col gap-2 rounded-lg border border-dashed p-3">
      <div className="flex items-center gap-2">
        <span className="text-xs font-medium">WhatsApp</span>
        {state.ready ? (
          <StatusBadge tone="success">Ready</StatusBadge>
        ) : state.state === "awaiting_number" ? (
          <StatusBadge tone="warning">Needs a number</StatusBadge>
        ) : (
          <StatusBadge tone="warning">Configured</StatusBadge>
        )}
      </div>

      {state.phone_number_id ? (
        <p className="text-xs text-muted-foreground">
          Sending from{" "}
          <span className="text-foreground">
            {state.display_number ?? state.phone_number_id}
          </span>
          . Choose a different number to switch.
        </p>
      ) : (
        <p className="text-xs text-muted-foreground">
          Choose the number messages are sent from.
        </p>
      )}

      {/* "Ready" is only claimed when this deployment could attempt a send, and the reason
          is named rather than left for the tenant to infer from silence. */}
      {!state.platform_available && (
        <p className="rounded-md border border-amber-200 bg-amber-50 p-2 text-xs text-amber-900 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-200">
          This installation has no Meta API credentials, so nothing can be sent from it yet.
          Your number is saved and will be used once they are configured.
        </p>
      )}

      {numbers.isError && (
        <InlineError error={numbers.error} onRetry={() => void numbers.refetch()} />
      )}
      {numbers.data && options.length === 0 && (
        <p className="text-xs text-muted-foreground">
          Meta reports no phone number on this WhatsApp Business Account. Add one in Meta,
          then reload.
        </p>
      )}

      {options.length > 0 && (
        <div className="flex flex-wrap items-center gap-2">
          <select
            aria-label="WhatsApp number"
            className="min-w-0 flex-1 rounded-md border bg-background px-2 py-1.5 text-sm focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
            value={effective}
            onChange={(event) => setChosen(event.target.value)}
          >
            {options.map((option) => (
              <option key={option.phone_number_id} value={option.phone_number_id}>
                {option.label}
              </option>
            ))}
          </select>
          <Button
            size="sm"
            disabled={
              selectNumber.isPending || !effective || effective === state.phone_number_id
            }
            onClick={() => selectNumber.mutate(effective)}
          >
            {selectNumber.isPending
              ? "Saving…"
              : state.phone_number_id
                ? "Switch number"
                : "Connect WhatsApp"}
          </Button>
        </div>
      )}
      {selectNumber.isError && (
        <InlineError error={selectNumber.error} onRetry={() => selectNumber.reset()} />
      )}
      {selectNumber.isSuccess && (
        <p className="text-xs text-muted-foreground">WhatsApp number saved.</p>
      )}
    </div>
  );
}

function MetaPanel() {
  const connections = useQuery(metaConnectionsOptions());
  const oauth = useStartOAuth();
  const disconnect = useDisconnectMeta();
  const [disconnecting, setDisconnecting] = useState<string | null>(null);

  if (connections.isPending) return <Skeleton className="h-40 w-full rounded-lg" />;
  if (connections.isError) {
    return (
      <ErrorState
        body="Could not reach the Meta integration."
        onRetry={() => void connections.refetch()}
      />
    );
  }
  const items = connections.data;
  if (items.length === 0) {
    return (
      <div className="flex flex-col gap-4">
        <EmptyState
          icon={Plug}
          title="No Meta account connected"
          body="Connect a Facebook Page or Instagram business account and QualiFly will start receiving leads and messages from it."
        />
        {oauth.error != null && <InlineError error={oauth.error} onRetry={oauth.reset} />}
        <div className="flex items-center justify-center">
          <Button size="sm" disabled={oauth.isPending} onClick={oauth.mutate}>
            {oauth.isPending ? "Opening Meta…" : "Connect Meta"}
          </Button>
        </div>
      </div>
    );
  }
  return (
    <div className="flex flex-col gap-3">
      {items.map((connection) => {
        const healthy = connection.status === "ACTIVE" && connection.access_token_configured;
        return (
        <div key={connection.id} className="flex flex-col gap-3 rounded-lg border p-4">
          <div className="flex flex-wrap items-center gap-3">
            <span
              aria-hidden
              className={
                healthy
                  ? "flex size-9 items-center justify-center rounded-full bg-emerald-50 text-emerald-600 dark:bg-emerald-950 dark:text-emerald-400"
                  : "flex size-9 items-center justify-center rounded-full bg-amber-50 text-amber-700 dark:bg-amber-950 dark:text-amber-400"
              }
            >
              {healthy ? <CheckCircle2 className="size-4" /> : <Plug className="size-4" />}
            </span>
            <span className="flex min-w-0 flex-1 flex-col">
              <span className="truncate text-sm font-medium">
                {connection.display_name ?? connection.page_id ?? "Meta account"}
              </span>
              <span className="text-xs text-muted-foreground">
                {connection.status === "ACTIVE"
                  ? connection.last_webhook_at
                    ? `Last message received ${relativeTime(connection.last_webhook_at)}`
                    : "Connected. No messages received yet."
                  : `Status: ${connection.status.toLowerCase()}. This account is not receiving leads or messages.`}
              </span>
            </span>
            {healthy ? (
              <StatusBadge tone="success">Connected</StatusBadge>
            ) : (
              <Button size="sm" disabled={oauth.isPending} onClick={oauth.mutate}>
                {oauth.isPending ? "Opening Meta…" : "Reconnect"}
              </Button>
            )}
            <Button
              size="sm"
              variant="outline"
              disabled={disconnecting === connection.id}
              onClick={() => {
                setDisconnecting(connection.id);
                disconnect.reset();
                disconnect.mutate(connection.id, {
                  onSettled: () => setDisconnecting(null),
                });
              }}
            >
              {disconnecting === connection.id ? "Disconnecting…" : "Disconnect"}
            </Button>
          </div>
          {connection.page_id && (
            <InstagramSection connectionId={connection.id} />
          )}
          <WhatsAppSection />
        </div>
        );
      })}
      {oauth.error != null && <InlineError error={oauth.error} onRetry={oauth.reset} />}
      {disconnect.isError && (
        <InlineError error={disconnect.error} onRetry={() => disconnect.reset()} />
      )}
      <p className="text-xs text-muted-foreground">
        Access tokens are stored encrypted and never shown here. Last verified{" "}
        {absoluteTime(new Date())}.
      </p>
    </div>
  );
}

function Row({ label, value, hint }: { label: string; value: React.ReactNode; hint?: string }) {
  return (
    <div className="flex flex-col gap-1 p-4 sm:flex-row sm:gap-4">
      <dt className="w-40 shrink-0 text-sm text-muted-foreground">{label}</dt>
      <dd className="min-w-0 text-sm">
        {value}
        {hint && <span className="mt-1 block text-xs text-muted-foreground">{hint}</span>}
      </dd>
    </div>
  );
}
