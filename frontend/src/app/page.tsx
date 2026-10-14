import { redirect } from "next/navigation";

/**
 * The product's front door is the dashboard.
 *
 * `/` redirects rather than rendering its own page, so there is exactly one answer to "what
 * does this app open on" and no second copy of the dashboard to keep in step. Someone who
 * lands on the root during development gets the real thing, not a splash screen.
 */
export default function Home() {
  redirect("/dashboard");
}
