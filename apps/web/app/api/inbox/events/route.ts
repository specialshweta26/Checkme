import { NextRequest } from "next/server";
import { apiBase } from "../../../../lib/backend";

export const dynamic = "force-dynamic";
export const maxDuration = 300;

export async function GET(req: NextRequest) {
  const cookie = req.headers.get("cookie") || "";
  const upstream = await fetch(`${apiBase()}/api/inbox/events`, {
    headers: { cookie, accept: "text/event-stream" },
    cache: "no-store",
  });
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
