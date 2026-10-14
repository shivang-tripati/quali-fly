import type { Metadata } from "next";

import { DashboardView } from "@/components/dashboard/dashboard-view";

export const metadata: Metadata = { title: "Dashboard · QualiFly" };

/**
 * The dashboard.
 *
 * A server component that renders one client view. The split is deliberate: the route
 * contributes the page-level metadata and stays out of the interactivity, so the bundle that
 * hydrates is the view and not the whole page tree. The data comes from one aggregated read
 * (§ the dashboard endpoint) rather than six parallel list queries, because every number on
 * this screen is a count over the same tables and counting them in the browser would mean
 * shipping every lead to render a number.
 */
export default function DashboardPage() {
  return <DashboardView />;
}
