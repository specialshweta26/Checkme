import { NextRequest } from "next/server";
import { proxyApi } from "../../../lib/proxy";

export const dynamic = "force-dynamic";
// Long agent turns stream heartbeats until the reply; the client recovers by progress id if this is cut.
export const maxDuration = 900;

export async function POST(req: NextRequest) {
  return proxyApi(req, "/api/chat");
}
