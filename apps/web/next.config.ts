import type { NextConfig } from "next";

const api =
  process.env.API_INTERNAL_URL ||
  (process.env.API_HOSTPORT ? `http://${process.env.API_HOSTPORT}` : "http://localhost:8000");

const nextConfig: NextConfig = {
  experimental: {
    proxyTimeout: 120000,
  },
  async rewrites() {
    return [
      { source: "/api/:path*", destination: `${api}/api/:path*` },
      { source: "/health", destination: `${api}/health` },
    ];
  },
};

export default nextConfig;
