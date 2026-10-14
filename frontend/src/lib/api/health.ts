import { queryOptions } from "@tanstack/react-query";
import { z } from "zod";

import { apiRequest } from "@/lib/api/client";

const healthSchema = z.object({ status: z.string() });
const readinessSchema = z.object({
  status: z.string(),
  checks: z.record(z.string(), z.string()),
});

export type Health = z.infer<typeof healthSchema>;
export type Readiness = z.infer<typeof readinessSchema>;

/** Liveness. Lives at the server root so it survives API versioning. */
export const getHealth = () => apiRequest("/health", healthSchema);

/** Readiness: adds a PostgreSQL round trip. */
export const getReadiness = () => apiRequest("/health/ready", readinessSchema);

export const healthQueryOptions = (options?: { refetchInterval?: number }) =>
  queryOptions({
    queryKey: ["health"] as const,
    queryFn: getHealth,
    retry: false,
    ...options,
  });
