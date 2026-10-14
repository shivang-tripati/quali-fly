import type { Metadata } from "next";

import { HandoffsView } from "@/components/handoffs/handoffs-view";

export const metadata: Metadata = { title: "Handoffs · QualiFly" };

export default function HandoffsPage() {
  return <HandoffsView />;
}
