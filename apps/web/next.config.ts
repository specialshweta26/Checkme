import type { NextConfig } from "next";

// No Content-Security-Policy is set, so the computer rail can embed the E2B Desktop noVNC stream
// (https://6080-<sandbox>.e2b.app). If a CSP is added later, frame-src must allow https://*.e2b.app and
// https://*.e2b.dev, plus the Composio connect pages framed by ConnectCard.
const nextConfig: NextConfig = {
  experimental: {
    proxyTimeout: 120000,
  },
  // Sign-in links carry one-time tokens in the URL: send no referrer from any page, so a token never leaks to another
  // site. nosniff stops content-type guessing; HSTS keeps browsers on HTTPS (Render serves only HTTPS).
  async headers() {
    const headers = [
      { key: "Referrer-Policy", value: "no-referrer" },
      { key: "X-Content-Type-Options", value: "nosniff" },
    ];
    if (process.env.NODE_ENV === "production") {
      headers.push({ key: "Strict-Transport-Security", value: "max-age=31536000; includeSubDomains" });
    }
    return [{ source: "/:path*", headers }];
  },
};

export default nextConfig;
