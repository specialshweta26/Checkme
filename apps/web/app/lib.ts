export type Me = {
  user_id: string;
  email: string;
  name: string;
  primary_provider: string;
  timezone?: string;
  accounts: { provider: string; email: string }[];
  connectors: { id: string; name: string; slug: string; provider: string; health: string; enabled: boolean }[];
};

export type ChatSessionRow = {
  id: string;
  title: string;
  updated_at: string;
};

export async function getMe(): Promise<Me | null> {
  const res = await fetch("/api/me", { credentials: "include" });
  if (res.status === 401) return null;
  if (!res.ok) return null;
  return res.json();
}

export async function listSessions(): Promise<ChatSessionRow[]> {
  const res = await fetch("/api/chat/sessions", { credentials: "include" });
  if (!res.ok) return [];
  const data = await res.json();
  return data.sessions || [];
}

export async function loadSession(id: string) {
  const res = await fetch(`/api/chat/sessions/${id}`, { credentials: "include" });
  if (!res.ok) return null;
  return res.json();
}

export async function deleteSession(id: string) {
  const res = await fetch(`/api/chat/sessions/${id}`, { method: "DELETE", credentials: "include" });
  return res.ok;
}

export type InboxAlert = {
  id: string;
  subject: string;
  from: string;
  date: string;
  classification: string;
  unread?: boolean;
  seen?: boolean;
};

export type InboxBadge = {
  count: number;
  pending: number;
  items: InboxAlert[];
  inbox?: any[];
  connected?: boolean;
};

export type UploadedFile = {
  key: string;
  filename: string;
  content_type?: string;
  kind?: string;
  size?: number;
  download_url?: string;
  preview_url?: string;
  previewable?: boolean;
  extracted_text?: string;
};

export async function uploadFiles(files: File[]): Promise<UploadedFile[]> {
  if (!files.length) return [];
  const body = new FormData();
  for (const file of files) body.append("files", file);
  const res = await fetch("/api/files/upload", { method: "POST", credentials: "include", body });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Upload failed");
  return data.files || [];
}

export function subscribeInbox(onData: (data: InboxBadge) => void): () => void {
  const es = new EventSource("/api/inbox/events");
  let fallback: number | null = null;
  const apply = (data: InboxBadge) => onData(data);
  const handler = (ev: MessageEvent) => {
    try {
      apply(JSON.parse(ev.data));
    } catch {
      /* ignore malformed frames */
    }
  };
  es.addEventListener("inbox", handler);
  es.onmessage = handler;
  es.onerror = () => {
    if (fallback != null) return;
    fallback = window.setInterval(() => {
      inboxBadge().then(apply);
    }, 3000);
  };
  es.onopen = () => {
    if (fallback == null) return;
    window.clearInterval(fallback);
    fallback = null;
  };
  return () => {
    es.close();
    if (fallback != null) window.clearInterval(fallback);
  };
}

function readBadge(data: Partial<InboxBadge> | null | undefined): InboxBadge {
  const count = Number(data?.count || 0);
  return {
    count,
    pending: Number(data?.pending ?? count),
    items: data?.items || [],
  };
}

export async function inboxBadge(): Promise<InboxBadge> {
  try {
    const res = await fetch("/api/inbox/badge", { credentials: "include" });
    if (!res.ok) return { count: 0, pending: 0, items: [] };
    return readBadge(await res.json());
  } catch {
    return { count: 0, pending: 0, items: [] };
  }
}

export async function markInboxSeen(): Promise<InboxBadge> {
  try {
    const res = await fetch("/api/inbox/seen", { method: "POST", credentials: "include" });
    if (!res.ok) return { count: 0, pending: 0, items: [] };
    return readBadge(await res.json());
  } catch {
    return { count: 0, pending: 0, items: [] };
  }
}

export function initials(name?: string, email?: string) {
  const src = (name || email || "A").trim();
  const parts = src.split(/[\s.@]+/).filter(Boolean);
  return `${(parts[0]?.[0] || "A")}${(parts[1]?.[0] || "")}`.toUpperCase();
}

export function relativeTime(iso?: string) {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const mins = Math.max(0, Math.round((Date.now() - then) / 60000));
  if (mins < 1) return "Just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  if (days < 7) return `${days}d ago`;
  return new Date(iso).toLocaleDateString("en-IN", { day: "numeric", month: "short" });
}

export function errorFromQuery(search: string) {
  const q = new URLSearchParams(search);
  const err = q.get("error");
  if (!err) return "";
  const map: Record<string, string> = {
    invalid_state: "Sign-in expired. Try again.",
    oauth_failed: "The provider refused sign-in. Check the OAuth app settings.",
    bad_client_secret:
      "Microsoft rejected the client secret. In Azure → App registrations → Certificates & secrets, create a new client secret and paste the Secret Value (the long string shown once), not the Secret ID (the GUID).",
    bad_redirect_uri:
      "Microsoft rejected the app or redirect URI. Add http://localhost:3000/api/auth/microsoft/callback under Azure Authentication → Redirect URIs (Web).",
    unauthorized_client:
      "Microsoft does not recognize this app for personal Outlook accounts. In Azure App registration Overview, copy the full Application (client) ID (8-4-4-4-12). Under Authentication, set Supported account types to “Accounts in any organizational directory and personal Microsoft accounts”, add redirect URI http://localhost:3000/api/auth/microsoft/callback, then update .env.",
    access_denied:
      "Google blocked the app because it is in Testing and this account is not a test user. Add the account under Google Auth Platform → Audience → Test users, then try again.",
    signin_required: "Sign in first, then connect the other account.",
    account_linked_elsewhere: "That mailbox is already linked to another user.",
    missing_connector: "That MCP connection was not found.",
  };
  return map[err] || err.replaceAll("_", " ");
}
