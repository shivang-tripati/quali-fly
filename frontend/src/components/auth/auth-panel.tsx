"use client";

import { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";

import { Button } from "@/components/ui/button";
import { InlineError } from "@/components/ui/states";
import { useLogin, useRegister, MIN_PASSWORD_LENGTH } from "@/lib/api/identity";

export type AuthMode = "signin" | "signup";

const COPY: Record<AuthMode, { title: string; blurb: string; submit: string; pending: string }> = {
  signin: {
    title: "Sign in",
    blurb: "Welcome back. Everything here is scoped to your workspace.",
    submit: "Sign in",
    pending: "Signing in…",
  },
  signup: {
    title: "Create your account",
    blurb: "Set up a workspace and start receiving leads. Takes a minute, and you can invite your team once you are in.",
    submit: "Create account",
    pending: "Creating your workspace…",
  },
};

/**
 * The sign-in and sign-up form.
 *
 * **One component, two routes.** `mode` is a prop rather than local state because the two
 * are separate pages with separate URLs, separate titles and separate reasons to exist. A
 * toggle that swapped the fields in place would have made `/signin` unreachable by link -
 * you could only arrive at it by first rendering something else - and the browser back
 * button would have walked through the form's history instead of leaving the page.
 *
 * The cross-link between them is a `<Link>`, so switching is a real navigation: address bar
 * updates, the document title changes, and back works.
 *
 * **Both forms are real forms.** `POST /api/v1/auth/register` and `/auth/login` take JSON
 * bodies, so an `<a href>` to either path would be a GET and a 405 - a control that looks
 * like it works and cannot.
 *
 * Client-side validation mirrors the server's rules and nothing more. The server is the
 * authority; this exists so an obvious mistake is caught before a round trip, not so the
 * browser becomes a second source of truth about what a valid request is.
 */
export function AuthPanel({ mode }: { mode: AuthMode }) {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [name, setName] = useState("");
  const [workspaceName, setWorkspaceName] = useState("");

  const isSignUp = mode === "signup";
  const copy = COPY[mode];

  const login = useLogin();
  const register = useRegister();
  const mutation = isSignUp ? register : login;
  const error = mutation.error;

  function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!isSignUp) {
      login.mutate({ email: email.trim(), password });
      return;
    }
    register.mutate(
      {
        name: name.trim(),
        email: email.trim(),
        password,
        workspace_name: workspaceName.trim(),
      },
      // Straight to onboarding rather than the dashboard. The dashboard is a real screen
      // full of zeros for a workspace that cannot yet receive anything, which reads as
      // broken; the welcome page says what to do next instead.
      { onSuccess: () => router.push("/welcome") },
    );
  }

  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-col gap-1.5">
        <h1 className="text-2xl font-semibold tracking-tight">{copy.title}</h1>
        <p className="text-sm text-muted-foreground">{copy.blurb}</p>
      </div>

      <form className="flex flex-col gap-3" onSubmit={submit}>
        {isSignUp && (
          <>
            <Field
              id="signup-name"
              label="Your name"
              autoComplete="name"
              value={name}
              onChange={setName}
              required
            />
            <Field
              id="signup-workspace"
              label="Workspace name"
              autoComplete="organization"
              value={workspaceName}
              onChange={setWorkspaceName}
              required
            />
          </>
        )}

        <Field
          id="auth-email"
          label="Email"
          type="email"
          autoComplete="username"
          value={email}
          onChange={setEmail}
          required
        />

        <div className="flex flex-col gap-1.5">
          <label htmlFor="auth-password" className="text-sm font-medium">
            Password
          </label>
          <input
            id="auth-password"
            type="password"
            autoComplete={isSignUp ? "new-password" : "current-password"}
            required
            minLength={isSignUp ? MIN_PASSWORD_LENGTH : undefined}
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            className="rounded-md border bg-background px-3 py-2 text-sm focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
          />
          {isSignUp && (
            // Stated up front, because a rule the user only meets as a red box after
            // submitting is a rule they experience as the form refusing them.
            <p className="text-xs text-muted-foreground">
              At least {MIN_PASSWORD_LENGTH} characters.
            </p>
          )}
        </div>

        {error != null && <InlineError error={error} onRetry={() => mutation.reset()} />}

        <Button type="submit" disabled={mutation.isPending}>
          {mutation.isPending ? copy.pending : copy.submit}
        </Button>
      </form>

      <p className="text-sm text-muted-foreground">
        {isSignUp ? "Already have an account? " : "New here? "}
        <Link
          href={isSignUp ? "/signin" : "/signup"}
          className="text-primary underline underline-offset-4 hover:no-underline"
        >
          {isSignUp ? "Sign in" : "Create an account"}
        </Link>
      </p>
    </div>
  );
}

function Field({
  id,
  label,
  value,
  onChange,
  type = "text",
  autoComplete,
  required,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (value: string) => void;
  type?: string;
  autoComplete?: string;
  required?: boolean;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={id} className="text-sm font-medium">
        {label}
      </label>
      <input
        id={id}
        type={type}
        autoComplete={autoComplete}
        required={required}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="rounded-md border bg-background px-3 py-2 text-sm focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
      />
    </div>
  );
}
