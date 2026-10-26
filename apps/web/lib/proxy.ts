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
  const t0 = Date.now();
  const chat = path.includes("/chat");
  // #region agent log
  const _dbg = (message: string, data: Record<string, unknown>, hypothesisId: string) => {
    const body = JSON.stringify({sessionId:'daee2b',location:'lib/proxy.ts:proxyApi',message,data,timestamp:Date.now(),hypothesisId,runId:'pre-fix'});
    fetch('http://127.0.0.1:7755/ingest/7762fe36-1f54-4ec2-b56d-3aaac63261c6',{method:'POST',headers:{'Content-Type':'application/json','X-Debug-Session-Id':'daee2b'},body}).catch(()=>{
      fetch('http://host.docker.internal:7755/ingest/7762fe36-1f54-4ec2-b56d-3aaac63261c6',{method:'POST',headers:{'Content-Type':'application/json','X-Debug-Session-Id':'daee2b'},body}).catch(()=>{});
    });
  };
  if (chat) _dbg('proxy start', {path, method: req.method}, 'A');
  // #endregion
  try {
    const upstream = await fetch(`${apiBase()}${path}${search}`, init);
    // #region agent log
    if (chat) _dbg('proxy upstream headers', {path, status: upstream.status, ms: Date.now() - t0}, 'A');
    // #endregion
    const out = new Headers();
    upstream.headers.forEach((value, key) => {
      if (!HOP.has(key.toLowerCase())) out.append(key, value);
    });
    return new Response(upstream.body, { status: upstream.status, headers: out });
  } catch (err: any) {
    // #region agent log
    if (chat) _dbg('proxy error', {path, name: err?.name || '', msg: String(err?.message || err?.cause || '').slice(0, 200), code: err?.code || err?.cause?.code || '', ms: Date.now() - t0}, 'A');
    // #endregion
    throw err;
  }
}
