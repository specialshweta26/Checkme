import type { NextConfig } from "next";

// No Content-Security-Policy is set, so the computer rail can embed the E2B Desktop noVNC stream
// (https://6080-<sandbox>.e2b.app). If a CSP is added later, frame-src must allow https://*.e2b.app and
// https://*.e2b.dev, plus the Composio connect pages framed by ConnectCard.
const nextConfig: NextConfig = {
  experimental: {
    proxyTimeout: 120000,
  },
};

export default nextConfig;
