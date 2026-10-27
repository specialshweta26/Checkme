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
  agent_id?: string | null;
  group_id?: string | null;
};

export type WorkspaceAgent = {
  id: string;
  name: string;
  title?: string;
  instructions?: string;
  focus?: string;
  focus_label?: string;
  color: string;
  kind?: string;
  pinned?: boolean;
  archived?: boolean;
  unread?: boolean;
  session_id?: string;
  preview?: string;
  updated_at?: string;
};

export type AgentGroup = {
  id: string;
  name: string;
  outcome?: string;
  owner_id?: string | null;
  members: WorkspaceAgent[];
  session_id?: string;
  preview?: string;
  updated_at?: string;
};

export type AgentSkill = {
  id: string;
  agent_id?: string | null;
  name: string;
  kind: string;
  instruction?: string;
  steps?: unknown[];
  duration_ms?: number;
};

export type WorkspaceRoster = {
  agents: WorkspaceAgent[];
  groups: AgentGroup[];
  default_agent_id?: string;
  connector_assignments?: Record<string, string[]>;
  toolkit_assignments?: Record<string, string[]>;
};

/** Fired on window when a chat turn created, changed, or deleted agents or groups. */
export const WORKSPACE_CHANGED_EVENT = "checkme:workspace-changed";

export function announceWorkspaceChanged() {
  if (typeof window !== "undefined") window.dispatchEvent(new Event(WORKSPACE_CHANGED_EVENT));
}

export async function loadWorkspace(): Promise<WorkspaceRoster> {
  const res = await fetch("/api/agents", { credentials: "include" });
  if (!res.ok) return { agents: [], groups: [] };
  return res.json();
}

export function apiError(data: unknown, fallback: string) {
  const detail = data && typeof data === "object" ? (data as { detail?: unknown }).detail : undefined;
  if (typeof detail === "string" && detail.trim()) return detail;
  if (Array.isArray(detail)) {
    const first = detail
      .map((row) => (typeof row === "string" ? row : row && typeof row === "object" ? (row as { msg?: string }).msg : ""))
      .find((row) => row);
    if (first) return String(first);
  }
  return fallback;
}

export async function createAgent(body: { name: string; title?: string; instructions?: string; color?: string }) {
  const res = await fetch("/api/agents", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(apiError(data, "Could not create agent"));
  return data as WorkspaceAgent;
}

export async function deleteAgent(id: string) {
  const res = await fetch(`/api/agents/${id}`, { method: "DELETE", credentials: "include" });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Could not delete agent");
  return res.ok;
}

async function agentAction(path: string, method: string, body?: unknown) {
  const res = await fetch(path, {
    method,
    credentials: "include",
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(apiError(data, "Could not update agent"));
  return data;
}

export async function updateAgent(
  id: string,
  body: {
    name?: string;
    title?: string;
    instructions?: string;
    focus?: string;
    color?: string;
    pinned?: boolean;
    archived?: boolean;
    unread?: boolean;
  },
) {
  return agentAction(`/api/agents/${id}`, "PATCH", body) as Promise<WorkspaceAgent>;
}

export async function duplicateAgent(id: string) {
  return agentAction(`/api/agents/${id}/duplicate`, "POST") as Promise<WorkspaceAgent>;
}

export async function clearAgentChat(id: string) {
  return agentAction(`/api/agents/${id}/clear`, "POST");
}

export async function moveAgentToGroup(agentId: string, groupId: string) {
  return agentAction(`/api/agents/${agentId}/move`, "POST", { group_id: groupId });
}

export async function assignConnectorAgents(connectorId: string, agentIds: string[]) {
  return agentAction(`/api/agents/connector-assignments/${connectorId}`, "PUT", { agent_ids: agentIds }) as Promise<{
    connector_id: string;
    agent_ids: string[];
  }>;
}

export async function grantConnectorAgent(connectorId: string, agentId: string) {
  return agentAction(`/api/agents/connector-assignments/${connectorId}/grant`, "POST", { agent_id: agentId });
}

export async function assignToolkitAgents(toolkit: string, agentIds: string[]) {
  return agentAction(`/api/agents/toolkit-assignments/${encodeURIComponent(toolkit)}`, "PUT", { agent_ids: agentIds }) as Promise<{
    toolkit: string;
    agent_ids: string[];
  }>;
}

export async function grantToolkitAgent(toolkit: string, agentId: string) {
  return agentAction(`/api/agents/toolkit-assignments/${encodeURIComponent(toolkit)}/grant`, "POST", { agent_id: agentId });
}

export async function connectMarketplace(toolkit: string, agentId?: string, credentials?: Record<string, string>) {
  const res = await fetch("/api/connectors/marketplace/connect", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ toolkit, agent_id: agentId || undefined, credentials }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(apiError(data, "Could not start Composio sign-in"));
  return data as {
    redirect_url?: string;
    toolkit?: string;
    name?: string;
    logo?: string;
    connected?: boolean;
    needs_credentials?: boolean;
    fields?: { name: string; label?: string; secret?: boolean; description?: string }[];
  };
}

export function nextAssignedAgents(assigned: string[] | undefined, agentId: string, allAgentIds: string[]) {
  const restricted = assigned || [];
  const current = restricted.length ? restricted : allAgentIds;
  const checked = !restricted.length || restricted.includes(agentId);
  return checked ? current.filter((id) => id !== agentId) : [...current.filter((id) => id !== agentId), agentId];
}

export async function createGroup(body: { name: string; member_ids: string[]; outcome?: string; owner_id?: string | null }) {
  const res = await fetch("/api/agents/groups", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Could not create group");
  return data as AgentGroup;
}

export async function listSkills(agentId?: string) {
  const q = agentId ? `?agent_id=${encodeURIComponent(agentId)}` : "";
  const res = await fetch(`/api/skills${q}`, { credentials: "include" });
  if (!res.ok) return { skills: [] as AgentSkill[] };
  return res.json() as Promise<{ skills: AgentSkill[] }>;
}

export async function saveSkill(body: { name: string; agent_id?: string; instruction?: string }) {
  return agentAction("/api/skills", "POST", body) as Promise<AgentSkill>;
}

export async function startTeaching(agentId?: string) {
  return agentAction("/api/skills/teach/start", "POST", { agent_id: agentId });
}

export async function stopTeaching(name?: string) {
  return agentAction("/api/skills/teach/stop", "POST", { name: name || "" }) as Promise<AgentSkill>;
}

export async function deleteGroup(id: string) {
  const res = await fetch(`/api/agents/groups/${id}`, { method: "DELETE", credentials: "include" });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Could not delete group");
  return res.ok;
}

export const LAST_AGENT_KEY = "assistant.lastAgent";
export const LAST_GROUP_KEY = "assistant.lastGroup";

export function chatHref(opts: { agent?: string | null; group?: string | null; conversation?: string | null; fresh?: boolean } = {}) {
  const q = new URLSearchParams();
  if (opts.group) q.set("group", opts.group);
  else if (opts.agent) q.set("agent", opts.agent);
  if (opts.conversation) q.set("c", opts.conversation);
  if (opts.fresh) q.set("new", "1");
  const search = q.toString();
  return search ? `/chat?${search}` : "/chat";
}

export function rememberScope(agentId?: string | null, groupId?: string | null) {
  if (typeof window === "undefined") return;
  if (groupId) {
    window.localStorage.setItem(LAST_GROUP_KEY, groupId);
    window.localStorage.removeItem(LAST_AGENT_KEY);
    return;
  }
  if (agentId) {
    window.localStorage.setItem(LAST_AGENT_KEY, agentId);
    window.localStorage.removeItem(LAST_GROUP_KEY);
  }
}

export function rememberedScope() {
  if (typeof window === "undefined") return { agent: "", group: "" };
  return {
    agent: window.localStorage.getItem(LAST_AGENT_KEY) || "",
    group: window.localStorage.getItem(LAST_GROUP_KEY) || "",
  };
}

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
  recipient_role?: string;
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
    inbox: data?.inbox,
    connected: data?.connected,
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
    github_app_not_configured: "The Checkme GitHub App is not set up on this server yet. See docs/GITHUB_APP.md.",
    github_needs_user_authorization:
      "GitHub did not send an authorization code. Turn on “Request user authorization (OAuth) during installation” in the App settings.",
    github_installation_not_yours: "That GitHub installation is not visible to your GitHub account.",
    github_install_failed: "GitHub install could not be verified. Try again.",
    github_missing_installation: "GitHub did not say which installation to use. Try installing again.",
  };
  return map[err] || err.replaceAll("_", " ");
}
