import { NextRequest } from "next/server";
import { apiBase, apiUnavailable, fetchApi, neverReachedApi } from "../../../../lib/backend";

export const dynamic = "force-dynamic";
export const maxDuration = 300;

export async function GET(req: NextRequest) {
  const cookie = req.headers.get("cookie") || "";
  let upstream: Response;
  try {
    upstream = await fetchApi(
      `${apiBase()}/api/inbox/events`,
      { headers: { cookie, accept: "text/event-stream" }, cache: "no-store" },
      req.signal,
    );
  } catch (err) {
    if (!neverReachedApi(err)) throw err;
    return apiUnavailable("/api/inbox/events", err);
  }
  return new Response(upstream.body, {
    status: upstream.status,
    headers: {
      "Content-Type": "text/event-stream; charset=utf-8",
      "Cache-Control": "no-cache, no-transform",
      Connection: "keep-alive",
      "X-Accel-Buffering": "no",
    },
  });
}
