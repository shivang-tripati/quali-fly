import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Produces .next/standalone for the slim, non-root runtime image.
  output: "standalone",
};

export default nextConfig;
