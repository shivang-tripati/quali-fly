import type { Metadata } from "next";

import { AuthPanel } from "@/components/auth/auth-panel";

export const metadata: Metadata = { title: "Sign in" };

/**
 * `/signin` is a real route, not a mode of some other page.
 *
 * It was previously a branch inside the signed-out state of the application shell, which
 * meant it had no URL of its own: the sign-in form was only reachable by losing your session
 * or by loading some other page, and a visitor could not be linked to it. Sign in and sign
 * up are two entry points, so they are two addresses.
 */
export default function SignInPage() {
  return <AuthPanel mode="signin" />;
}
