import { NextRequest } from "next/server";
import { proxyApi } from "../../../lib/proxy";

export const dynamic = "force-dynamic";
export const maxDuration = 120;

export async function POST(req: NextRequest) {
  return proxyApi(req, "/api/chat");
}
