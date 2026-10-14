import Link from "next/link";

/**
 * The public frame for `/signin` and `/signup`.
 *
 * Deliberately almost empty. This screen has exactly two jobs - explain which form you are
 * looking at, and let you submit it - and every extra element is an obstacle between
 * someone and the form. Specifically absent, because each one of these is a control that
 * cannot work here:
 *
 *   * the workspace switcher  - it lists memberships, and a signed-out visitor has none
 *   * the sidebar and its nav  - every destination is tenant-scoped and would 403
 *   * the notification bell    - same, plus a poll that fires before a session exists
 *   * the command palette      - it searches tenant data for the same reason
 *   * the topbar search        - ditto
 *
 * The wordmark is the only brand element, and it is a `<Link>` to `/` rather than a heading,
 * so the logo is a real destination instead of decoration.
 *
 * Centred on a full-height surface with a hairline border around the form, which is enough
 * to read as "this is a separate thing" without inventing a second visual system.
 */
export default function AuthGroupLayout({ children }: LayoutProps<"/">) {
  return (
    <div className="flex min-h-dvh flex-col bg-muted/30">
      <header className="flex h-14 items-center px-4 lg:px-6">
        <Link href="/" className="text-sm font-semibold">
          QualiFly
        </Link>
      </header>

      <main className="flex flex-1 items-center justify-center px-4 py-10">
        <div className="w-full max-w-sm rounded-xl border bg-background p-6 shadow-sm">
          {children}
        </div>
      </main>
    </div>
  );
}
