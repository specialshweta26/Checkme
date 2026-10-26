import { NextRequest } from "next/server";
import { proxyApi } from "../../../lib/proxy";

export const dynamic = "force-dynamic";
export const maxDuration = 120;

export async function POST(req: NextRequest) {
  const t0 = Date.now();
  // #region agent log
  const _dbg = (message: string, data: Record<string, unknown>, hypothesisId: string) => {
    const body = JSON.stringify({sessionId:'daee2b',location:'api/chat/route.ts:POST',message,data,timestamp:Date.now(),hypothesisId,runId:'pre-fix'});
    fetch('http://127.0.0.1:7755/ingest/7762fe36-1f54-4ec2-b56d-3aaac63261c6',{method:'POST',headers:{'Content-Type':'application/json','X-Debug-Session-Id':'daee2b'},body}).catch(()=>{
      fetch('http://host.docker.internal:7755/ingest/7762fe36-1f54-4ec2-b56d-3aaac63261c6',{method:'POST',headers:{'Content-Type':'application/json','X-Debug-Session-Id':'daee2b'},body}).catch(()=>{});
    });
  };
  _dbg('route start', {maxDuration: 120}, 'A');
  // #endregion
  try {
    const res = await proxyApi(req, "/api/chat");
    // #region agent log
    _dbg('route done', {status: res.status, ms: Date.now() - t0}, 'A');
    // #endregion
    return res;
  } catch (err: any) {
    // #region agent log
    _dbg('route error', {name: err?.name || '', msg: String(err?.message || err?.cause || '').slice(0, 200), code: err?.code || err?.cause?.code || '', ms: Date.now() - t0}, 'A');
    // #endregion
    throw err;
  }
}
