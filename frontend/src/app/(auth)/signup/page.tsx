import type { Metadata } from "next";

import { AuthPanel } from "@/components/auth/auth-panel";

export const metadata: Metadata = { title: "Create your account" };

/**
 * `/signup` - the new-customer entry point.
 *
 * Separate from `/signin` so it can be linked to directly from a landing page, an ad, or a
 * support answer, and so that a visitor who is sure they do not have an account is never
 * shown a form that assumes they do.
 */
export default function SignUpPage() {
  return <AuthPanel mode="signup" />;
}
