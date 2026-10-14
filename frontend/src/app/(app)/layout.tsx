import type { Metadata } from "next";

import { AppLayout } from "@/components/layout/app-layout";

/**
 * The authenticated shell.
 *
 * The `(app)` group is the *whole* signed-in product: sidebar, topbar, notification bell,
 * command palette, workspace switcher. It is a route group rather than a path prefix, so
 * these pages are still `/dashboard`, `/leads` and the rest - nothing about their URLs
 * changed, and the group name appears in no bookmark.
 *
 * **Why a group and not a conditional on the shell.** The shell used to render the sign-in
 * form in place of `children` whenever there was no session, which meant the sidebar, the
 * workspace switcher and the nav were all on screen while someone was trying to create an
 * account. Every one of those controls fetches tenant-scoped data, so a signed-out visitor
 * was also paying for a burst of guaranteed `TENANT_ACCESS_DENIED` requests to render
 * chrome they could not use. A layout boundary fixes both: the auth pages under `(auth)`
 * have no shell at all, and this group is the only place the shell is mounted.
 *
 * One root layout still exists (`src/app/layout.tsx`, which owns `<html>` and `<body>`), so
 * moving between `(auth)` and `(app)` is a client-side transition rather than the full page
 * reload that multiple root layouts would cause.
 */
export const metadata: Metadata = {
  title: { default: "QualiFly", template: "%s — QualiFly" },
  description: "AI lead nurturing for Meta ads",
};

export default function AppGroupLayout({ children }: LayoutProps<"/">) {
  return <AppLayout>{children}</AppLayout>;
}
