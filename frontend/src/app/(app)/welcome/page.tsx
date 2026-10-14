import type { Metadata } from "next";

import { WelcomeView } from "@/components/auth/welcome-view";

export const metadata: Metadata = { title: "Welcome — QualiFly" };

/**
 * Where a new customer lands after registering.
 *
 * A real route rather than a modal or a redirect-then-navigate, so it is linkable and the
 * back button behaves. It is not forced: this is a page the shell renders like any other,
 * and every exit on it is a plain link. Nothing here can strand a user who came from an
 * expired session, because the shell only reaches `children` once a session exists.
 */
export default function WelcomePage() {
  return (
    <div className="mx-auto w-full max-w-6xl px-4 py-6 lg:px-6">
      <WelcomeView />
    </div>
  );
}
