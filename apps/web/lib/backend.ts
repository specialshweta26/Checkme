export function apiBase() {
  const raw = (process.env.API_INTERNAL_URL || process.env.API_HOSTPORT || "").trim();
  if (!raw) return "http://localhost:8000";
  if (raw.startsWith("http://") || raw.startsWith("https://")) return raw.replace(/\/$/, "");
  return `http://${raw}`;
}
