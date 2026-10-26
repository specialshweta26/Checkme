import { NextRequest } from "next/server";
import { apiBase } from "./backend";

const HOP = new Set([
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailers",
  "transfer-encoding",
  "upgrade",
  "host",
  "content-encoding",
  "content-length",
]);

export async function proxyApi(req: NextRequest, path: string) {
  const search = new URL(req.url).search;
  const headers = new Headers();
  req.headers.forEach((value, key) => {
    if (!HOP.has(key.toLowerCase())) headers.set(key, value);
  });
  const init: RequestInit = { method: req.method, headers, redirect: "manual" };
  if (req.method !== "GET" && req.method !== "HEAD") {
    init.body = await req.arrayBuffer();
  }
  const upstream = await fetch(`${apiBase()}${path}${search}`, init);
  const out = new Headers();
  upstream.headers.forEach((value, key) => {
    if (!HOP.has(key.toLowerCase())) out.append(key, value);
  });
  return new Response(upstream.body, { status: upstream.status, headers: out });
}
