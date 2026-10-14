import { z } from "zod";

/**
 * Public, browser-safe configuration.
 *
 * Only `NEXT_PUBLIC_*` values belong here — they are inlined into the client bundle
 * at build time. Secrets (DATABASE_URL, BETTER_AUTH_SECRET, META_APP_SECRET,
 * GROQ_API_KEY, ...) must never be referenced from frontend code.
 */
const clientEnvSchema = z.object({
  /** Backend origin. Empty string = same origin, proxied by Nginx. */
  NEXT_PUBLIC_API_BASE_URL: z.string().default(""),
});

export const env = clientEnvSchema.parse({
  NEXT_PUBLIC_API_BASE_URL: process.env.NEXT_PUBLIC_API_BASE_URL,
});
