"use client";

import type { ReactNode } from "react";
import { useState } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

let browserQueryClient: QueryClient | undefined;

function getQueryClient(): QueryClient {
  // Server renders get an isolated client; the browser reuses one across renders.
  if (typeof window === "undefined") return new QueryClient();
  browserQueryClient ??= new QueryClient({
    defaultOptions: { queries: { staleTime: 30_000, retry: 1 } },
  });
  return browserQueryClient;
}

export function Providers({
  children,
  client,
}: {
  children: ReactNode;
  /** Supply an explicit client (tests, or a server-driven cache). */
  client?: QueryClient;
}) {
  const [queryClient] = useState(() => client ?? getQueryClient());
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
}
