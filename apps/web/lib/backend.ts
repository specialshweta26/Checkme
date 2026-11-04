export function apiBase() {
  const raw = (process.env.API_INTERNAL_URL || process.env.API_HOSTPORT || "").trim();
  if (!raw) return "http://localhost:8000";
  if (raw.startsWith("http://") || raw.startsWith("https://")) return raw.replace(/\/$/, "");
  return `http://${raw}`;
}

// These mean the request never reached the API, so retrying is safe for any method, including POST.
// checkme-api has a persistent disk, so Render stops it before starting the new instance on every deploy.
const NOT_REACHED = new Set(["ENOTFOUND", "EAI_AGAIN", "ECONNREFUSED"]);
const RESTART_WAIT_MS = 45_000;

export function neverReachedApi(err: unknown): boolean {
  const code = (err as { cause?: { code?: string } } | null)?.cause?.code;
  return !!code && NOT_REACHED.has(code);
}

/** fetch to the API that waits out a restart. `clientGone` stops the wait if the browser has left. */
export async function fetchApi(url: string, init: RequestInit, clientGone?: AbortSignal, waitMs = RESTART_WAIT_MS) {
  const deadline = Date.now() + waitMs;
  let delay = 500;
  for (;;) {
    try {
      return await fetch(url, init);
    } catch (err) {
      if (!neverReachedApi(err) || clientGone?.aborted || Date.now() + delay > deadline) throw err;
      await new Promise((resolve) => setTimeout(resolve, delay));
      delay = Math.min(delay * 2, 4000);
    }
  }
}

export function apiUnavailable(path: string, err: unknown) {
  const code = (err as { cause?: { code?: string } } | null)?.cause?.code;
  console.warn(`API unavailable for ${path} (${code}): still down after waiting`);
  return Response.json(
    { detail: "Lumio is restarting, so this didn't go through. Please try again in a moment." },
    { status: 503, headers: { "Retry-After": "5" } },
  );
}
