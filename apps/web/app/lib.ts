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
  suggested_tasks?: { title: string; prompt: string }[];
  color: string;
  avatar?: string;
  kind?: string;
  pinned?: boolean;
  archived?: boolean;
  unread?: boolean;
  session_id?: string;
  preview?: string;
  updated_at?: string;
  lessons?: { id: string; text: string }[];
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

export type SkillStatus = "recording" | "writing" | "ready";

export type SkillStep = {
  t?: number;
  kind?: string;
  text?: string;
  window?: string;
  key?: string;
  x?: number;
  y?: number;
  tool?: string;
  said?: string;
  masked?: boolean;
  [key: string]: unknown;
};

export type AgentSkill = {
  id: string;
  agent_id?: string | null;
  name: string;
  kind: string;
  status?: SkillStatus | string;
  error?: string;
  instruction?: string;
  steps?: SkillStep[];
  duration_ms?: number;
  created_at?: string;
  updated_at?: string;
  step_count?: number;
  frame_count?: number;
  has_transcript?: boolean;
  transcript?: { t?: number; text?: string }[];
  notes?: string[];
  frames?: { index: number; t?: number }[];
  flow?: import("./components/SkillFlow").Flow;
  description?: string;
  source?: SkillSource;
  files?: { path: string; size: number }[];
  revision?: number;
};

// Claude-format skills (apps/api/app/skills/claude_format.py, library.py; docs/SKILLS.md).
export type SkillSource = {
  type?: "github" | "upload";
  label?: string;
  url?: string;
  repo_url?: string;
  path?: string;
  ref?: string;
  sha?: string;
  name?: string;
  license?: string;
  imported_at?: string;
  filename?: string;
  skipped?: { path: string; why: string }[];
};
export type LibrarySource = { id: string; name: string; url: string; about: string };
export type LibrarySkill = {
  path: string;
  folder: string;
  name: string;
  title: string;
  description: string;
  license: string;
  category?: string;
  files: number;
  size: number;
  has_scripts: boolean;
  error?: string;
};
export type LibraryCatalog = {
  source: { owner: string; repo: string; ref: string; sha: string; path: string; url: string; label: string };
  skills: LibrarySkill[];
  more: number;
  truncated: boolean;
};
export type ImportResult = {
  imported: { skill: AgentSkill; created: boolean; skipped: { path: string; why: string }[] }[];
  errors: { path: string; error: string }[];
};

async function skillCall<T>(path: string, init: RequestInit, fallback: string): Promise<T> {
  const res = await fetch(path, { credentials: "include", ...init });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(apiError(data, fallback));
  return data as T;
}

export const skillLibrarySources = () => skillCall<{ sources: LibrarySource[] }>("/api/skills/library/sources", {}, "Couldn't load libraries");
export const skillLibrary = (url: string) => skillCall<LibraryCatalog>(`/api/skills/library?url=${encodeURIComponent(url)}`, {}, "Couldn't read that library");
export const importLibrarySkills = (body: { url: string; paths: string[]; ref?: string; agent_id?: string | null }) =>
  skillCall<ImportResult>("/api/skills/import", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }, "Couldn't import");
export function uploadSkill(file: File, agentId?: string | null) {
  const form = new FormData();
  form.append("file", file);
  if (agentId) form.append("agent_id", agentId);
  return skillCall<ImportResult>("/api/skills/import/upload", { method: "POST", body: form }, "Couldn't import that file");
}
export const skillUpdateFromSource = (id: string, check = false) =>
  skillCall<{ update: boolean; updated?: boolean; sha: string; skill: AgentSkill }>(`/api/skills/${id}/update${check ? "?check=true" : ""}`, { method: "POST" }, "Couldn't reach the library");
export const skillFileText = (id: string, path: string) =>
  skillCall<{ path: string; size: number; text: string }>(`/api/skills/${id}/file?path=${encodeURIComponent(path)}`, {}, "Couldn't open that file");
export const skillExportUrl = (id: string) => `/api/skills/${id}/export`;
/** Matches a library row to a skill already imported from it (same repository folder). */
export function importedFrom(skills: AgentSkill[], repoUrl: string, path: string) {
  const repo = repoUrl.toLowerCase();
  return skills.filter((s) => s.source?.type === "github" && (s.source.repo_url || "").toLowerCase() === repo && (s.source.path || "") === path);
}

export function skillBusy(skill: Pick<AgentSkill, "status"> | null | undefined) {
  return skill?.status === "recording" || skill?.status === "writing";
}

/** One recorded step as a short line (mirrors the API's narration.describe). */
export function describeSkillStep(step: SkillStep) {
  const kind = String(step.kind || "");
  const where = step.window ? ` in “${step.window}”` : "";
  if (kind === "say") return `said “${step.text || ""}”`;
  if (kind === "click") return `clicked${typeof step.x === "number" ? ` at (${step.x}, ${step.y})` : ""}${where}`;
  if (kind === "type" || kind === "input") return `typed “${step.text || ""}”${where}`;
  if (kind === "key") return `pressed ${step.key || step.text || "a key"}${where}`;
  if (kind === "scroll") return `scrolled${where}`;
  if (kind === "window") return `switched to “${step.window || step.text || "a window"}”`;
  if (kind === "tool") return `ran ${step.tool || "a tool"}`;
  return `${kind || "step"}${step.text ? ` “${step.text}”` : ""}${where}`;
}

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

let workspaceInflight: Promise<WorkspaceRoster> | null = null;

export async function loadWorkspace(): Promise<WorkspaceRoster> {
  if (!workspaceInflight) {
    workspaceInflight = fetch("/api/agents", { credentials: "include" })
      .then((res) => (res.ok ? res.json() : { agents: [], groups: [] }))
      .finally(() => {
        workspaceInflight = null;
      });
  }
  return workspaceInflight;
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

export async function createAgent(body: { name: string; title?: string; instructions?: string; color?: string; avatar?: string }) {
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
    avatar?: string;
    pinned?: boolean;
    archived?: boolean;
    unread?: boolean;
  },
) {
  return agentAction(`/api/agents/${id}`, "PATCH", body) as Promise<WorkspaceAgent>;
}

export type AgentOnboarding = {
  greeting: string;
  questions: string[];
  tasks: { title: string; prompt: string }[];
  skills: { title: string; prompt: string }[];
};

export async function getAgentOnboarding(id: string): Promise<AgentOnboarding> {
  const res = await fetch(`/api/agents/${id}/onboarding`, { credentials: "include" });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(apiError(data, "Could not prepare agent onboarding"));
  return data as AgentOnboarding;
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

export async function connectMarketplace(
  toolkit: string,
  agentId?: string,
  credentials?: Record<string, string>,
  agentIds?: string[],
) {
  const res = await fetch("/api/connectors/marketplace/connect", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      toolkit,
      agent_id: agentId || undefined,
      agent_ids: agentIds?.length ? agentIds : undefined,
      credentials,
    }),
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

export async function getSkill(id: string) {
  const res = await fetch(`/api/skills/${encodeURIComponent(id)}`, { credentials: "include", cache: "no-store" });
  const data = await res.json().catch(() => ({}));
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(apiError(data, "Could not load the skill"));
  return data as AgentSkill;
}

export async function updateSkill(id: string, body: { name?: string; instruction?: string; flow?: { nodes: unknown[] } }) {
  return agentAction(`/api/skills/${encodeURIComponent(id)}`, "PATCH", body) as Promise<AgentSkill>;
}

export async function deleteSkill(id: string) {
  return agentAction(`/api/skills/${encodeURIComponent(id)}`, "DELETE") as Promise<{ ok: boolean }>;
}

export async function rewriteSkill(id: string) {
  return agentAction(`/api/skills/${encodeURIComponent(id)}/rewrite`, "POST") as Promise<AgentSkill>;
}

export function skillFrameUrl(id: string, index: number) {
  return `/api/skills/${encodeURIComponent(id)}/frames/${index}`;
}

/** The theme the app shell saved ("lumio.theme"); dark when nothing is saved, as in AppShell. */
export const THEME_KEY = "lumio.theme";
export function savedTheme(): "dark" | "light" {
  try {
    return window.localStorage.getItem(THEME_KEY) === "light" ? "light" : "dark";
  } catch {
    return "dark";
  }
}

/** Pages outside the app shell (sign-in) wear the same theme: toggles body.app-dark and undoes it on unmount. */
export function applySavedTheme() {
  document.body.classList.toggle("app-dark", savedTheme() === "dark");
  return () => document.body.classList.remove("app-dark");
}

/** Whether the API offers the local demo sign-in (DEV_LOGIN on a localhost PUBLIC_WEB_URL only). */
export async function devLoginAvailable(): Promise<boolean> {
  try {
    const res = await fetch("/api/auth/status", { credentials: "include" });
    if (!res.ok) return false;
    const data = await res.json();
    return data?.dev_login === true;
  } catch {
    return false;
  }
}

export function isLumioDesktop() {
  return typeof window !== "undefined" && Boolean((window as { __lumioDesktop?: boolean }).__lumioDesktop);
}

function downloadName(href: string, header: string | null, fallback: string) {
  const match = /filename\*=UTF-8''([^;]+)|filename="([^"]+)"/i.exec(header || "");
  const named = decodeURIComponent(match?.[1] || match?.[2] || "");
  if (named) return named;
  if (fallback) return fallback;
  try {
    const last = new URL(href, window.location.origin).pathname.split("/").pop() || "";
    return decodeURIComponent(last) || "download";
  } catch {
    return "download";
  }
}

/** Save a chat file. The desktop webview swallows same-site window.open, so that app hands the link to the system browser. */
export async function saveDownload(href: string, filename = "") {
  const url = (href || "").trim();
  if (!url || typeof window === "undefined") return;
  const tauri = (window as { __TAURI__?: { core?: { invoke?: (name: string, args: { url: string }) => Promise<unknown> } } }).__TAURI__?.core;
  if (isLumioDesktop() && tauri?.invoke && /^https?:\/\//i.test(url)) {
    await tauri.invoke("open_external", { url });
    return;
  }
  const res = await fetch(url, { credentials: "include" });
  if (!res.ok) throw new Error("Could not download this file");
  const blob = await res.blob();
  const objectUrl = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = objectUrl;
  link.download = downloadName(url, res.headers.get("content-disposition"), filename);
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1500);
}

export type TeachStart = { ok?: boolean; teaching?: boolean; max_seconds?: number; elapsed?: number; audio_optional?: boolean; skill_id?: string | null };

export async function startTeaching(agentId?: string, page?: { url?: string; title?: string }) {
  return agentAction("/api/skills/teach/start", "POST", {
    agent_id: agentId,
    page_url: page?.url || "",
    page_title: page?.title || "",
  }) as Promise<TeachStart>;
}

export async function stopTeaching(name?: string) {
  return agentAction("/api/skills/teach/stop", "POST", { name: name || "" }) as Promise<AgentSkill & { writing?: boolean; narrated?: boolean; recovered?: boolean }>;
}

export async function teachingStatus() {
  const res = await fetch("/api/skills/teach/status", { credentials: "include", cache: "no-store" });
  if (!res.ok) return { teaching: false } as { teaching: boolean; max_seconds?: number; elapsed?: number; remaining?: number };
  return res.json() as Promise<{ teaching: boolean; max_seconds?: number; elapsed?: number; remaining?: number }>;
}

/** File extension the API (and OpenAI transcription) accepts for a MediaRecorder blob type. */
export function audioExtension(type: string) {
  const base = (type || "").split(";")[0].trim().toLowerCase();
  if (base.endsWith("/webm")) return "webm";
  if (base.endsWith("/ogg")) return "ogg";
  if (base === "audio/aac" || base === "audio/x-m4a") return "m4a";
  if (base.endsWith("/mpeg")) return "mp3";
  if (base.endsWith("/wav")) return "wav";
  return "mp4"; // iOS Safari records audio/mp4 (AAC)
}

/** Upload one narration clip recorded while teaching. offsetMs = when the clip started, since teaching began. */
export async function uploadNarration(blob: Blob, offsetMs: number, index = 0) {
  const form = new FormData();
  form.append("audio", blob, `narration-${index}.${audioExtension(blob.type)}`);
  form.append("offset_ms", String(Math.max(0, Math.round(offsetMs))));
  const res = await fetch("/api/skills/teach/audio", { method: "POST", credentials: "include", body: form });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(apiError(data, "Could not upload the narration"));
  return data as { ok: boolean; clips: number };
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

let meInflight: Promise<Me | null> | null = null;

// Only a 401 means signed out. A server restart (a deploy) or a network blip answers 502/503 or nothing for a few
// seconds: retry instead of sending the user to the sign-in page.
async function fetchMe(): Promise<Me | null> {
  for (let attempt = 0; attempt < 6; attempt++) {
    try {
      const res = await fetch("/api/me", { credentials: "include", signal: AbortSignal.timeout(8000) });
      if (res.status === 401 || res.status === 403) return null;
      if (res.ok) return res.json();
    } catch {
      // network error or timeout: try again
    }
    await new Promise((resolve) => setTimeout(resolve, 2000 + attempt * 1000));
  }
  return null;
}

export async function getMe(): Promise<Me | null> {
  if (!meInflight) {
    meInflight = fetchMe().finally(() => {
      meInflight = null;
    });
  }
  return meInflight;
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
  };
  return map[err] || err.replaceAll("_", " ");
}

export type WhatsAppLink = {
  id: string;
  channel: string;
  address: string;
  display_name: string;
  sticky_agent: string | null;
  linked_at: string;
  last_inbound_at: string;
  window_open: boolean;
  queued: number;
};

export type WhatsAppStatus = { enabled: boolean; business_number: string; wa_link: string; links: WhatsAppLink[] };

export type WhatsAppCode = { code: string; message: string; expires_at: string; wa_link: string; business_number: string };

export async function whatsappStatus(): Promise<WhatsAppStatus | null> {
  const res = await fetch("/api/channels/whatsapp", { credentials: "include", cache: "no-store" });
  if (!res.ok) return null;
  return res.json() as Promise<WhatsAppStatus>;
}

export async function whatsappLinkCode(): Promise<WhatsAppCode> {
  const res = await fetch("/api/channels/whatsapp/link-code", { method: "POST", credentials: "include" });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(apiError(data, "Could not create a link code"));
  return data as WhatsAppCode;
}

export async function whatsappUnlink(id: string) {
  const res = await fetch(`/api/channels/whatsapp/links/${encodeURIComponent(id)}`, { method: "DELETE", credentials: "include" });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(apiError(data, "Could not disconnect WhatsApp"));
  return true;
}

// Routines: scheduled tasks per agent, their run history and the dashboard (apps/api/app/tasks; docs/ROUTINES.md).
export type Routine = {
  id: string;
  agent_id?: string | null;
  agent_name?: string;
  agent_color?: string;
  chat_session_id?: string | null;
  title: string;
  instruction: string;
  prompt: string;
  schedule_kind: string;
  schedule_label: string;
  next_run_at?: string | null;
  next_run_label: string;
  last_run_at?: string | null;
  last_run_label: string;
  last_result: string;
  last_error: string;
  status: string;
  connected_apps: string[];
  run_count: number;
};
export type RoutineRunStatus = "succeeded" | "failed" | "needs_approval";
export type RoutineRun = {
  id: string;
  task_id: string;
  task_title: string;
  agent_id?: string | null;
  agent_name: string;
  agent_color: string;
  session_id?: string | null;
  job_id?: string | null;
  status: RoutineRunStatus;
  trigger: "schedule" | "manual" | "earlier";
  started_at: string;
  started_label: string;
  duration_ms: number | null;
  result: string;
  error: string;
};
export type RoutineAgentStats = { agent_id: string | null; agent_name: string; agent_color: string; routines: number; active: number; succeeded: number; failed: number; needs_approval: number };
export type RoutineSummary = {
  days: number;
  runs: number;
  succeeded: number;
  failed: number;
  needs_approval: number;
  success_rate: number | null;
  routines: number;
  active: number;
  paused: number;
  last_failure: RoutineRun | null;
  agents: RoutineAgentStats[];
};

const qs = (params: Record<string, string | number | null | undefined>) => {
  const q = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v !== null && v !== undefined && v !== "") q.set(k, String(v));
  });
  const text = q.toString();
  return text ? `?${text}` : "";
};

export const listRoutines = (agentId?: string | null) => skillCall<{ tasks: Routine[] }>(`/api/tasks${qs({ agent_id: agentId })}`, {}, "Couldn't load routines");
export const routineSummary = (agentId?: string | null, days = 30) => skillCall<RoutineSummary>(`/api/tasks/summary${qs({ agent_id: agentId, days })}`, {}, "Couldn't load the dashboard");
export const routineRuns = (opts: { agentId?: string | null; status?: string; days?: number; taskId?: string; limit?: number } = {}) =>
  skillCall<{ runs: RoutineRun[] }>(
    `/api/tasks/runs${qs({ agent_id: opts.agentId, status: opts.status, days: opts.days ?? 30, task_id: opts.taskId, limit: opts.limit })}`,
    {},
    "Couldn't load run history",
  );
export const createRoutine = (prompt: string, agentId?: string | null) =>
  skillCall<Routine>("/api/tasks", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ prompt, agent_id: agentId || undefined }) }, "Couldn't schedule that");
export const routineRunNow = (id: string) => skillCall<Routine>(`/api/tasks/${id}/run`, { method: "POST" }, "Couldn't run it");
export const routineSetStatus = (id: string, status: "active" | "paused") =>
  skillCall<Routine>(`/api/tasks/${id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ status }) }, "Couldn't change it");
export const routineDelete = (id: string) => skillCall<{ ok: boolean }>(`/api/tasks/${id}`, { method: "DELETE" }, "Couldn't delete it");

// Quality loop: ratings on replies and how each agent is doing (apps/api/app/quality; docs/QUALITY.md).
export type QualityReasonKey = "wrong" | "incomplete" | "not_done" | "unwanted" | "slow" | "tone";
export const QUALITY_REASONS: { key: QualityReasonKey; label: string }[] = [
  { key: "wrong", label: "Wrong or made up" },
  { key: "incomplete", label: "Incomplete" },
  { key: "not_done", label: "Didn't do what I asked" },
  { key: "unwanted", label: "Did something I didn't want" },
  { key: "slow", label: "Too slow" },
  { key: "tone", label: "Wrong tone or style" },
];
export type ReplyRating = {
  id: string;
  rating: number;
  reasons: string[];
  reason_labels: string[];
  note: string;
  prompt: string;
  reply: string;
  tools: string[];
  session_id: string;
  request_id: string;
  agent_id: string | null;
  agent_name: string;
  agent_color: string;
  at: string;
};
export type QualitySummary = {
  days: number;
  up: number;
  down: number;
  satisfaction: number | null;
  routines: { succeeded: number; failed: number; success_rate: number | null };
  drafts: { unchanged: number; light: number; rewritten: number; sent: number; kept_rate: number | null };
  reasons: { reason: string; label: string; count: number }[];
  agents: { agent_id: string | null; agent_name: string; agent_color: string; up: number; down: number; satisfaction: number | null; routine_ok: number; routine_failed: number }[];
};
export const rateReply = (body: { session_id: string; request_id: string; rating: number; reasons?: string[]; note?: string; agent_id?: string }) =>
  skillCall<{ rating: number; lesson?: string | null }>("/api/quality/feedback", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }, "Couldn't save your rating");
export const sessionRatings = (sessionId: string) => skillCall<{ ratings: Record<string, number> }>(`/api/quality/feedback/session/${sessionId}`, {}, "Couldn't load ratings");
export const qualitySummary = (days = 30, agentId = "") => skillCall<QualitySummary>(`/api/quality/summary${qs({ days, agent_id: agentId })}`, {}, "Couldn't load quality");
export const qualityFeedback = (opts: { rating?: number; agentId?: string; days?: number } = {}) =>
  skillCall<{ feedback: ReplyRating[] }>(`/api/quality/feedback${qs({ rating: opts.rating, agent_id: opts.agentId, days: opts.days ?? 30 })}`, {}, "Couldn't load ratings");
export const qualityExport = (ids: string[] = [], days = 90) => skillCall<{ cases: unknown[] }>(`/api/quality/export${qs({ ids: ids.join(","), days })}`, {}, "Couldn't export");

// Learning loop: lessons agents propose from a thumbs-down; accepted ones go into the agent's prompt.
export type AgentLesson = {
  id: string;
  agent_id: string;
  agent_name: string;
  agent_color: string;
  text: string;
  why: string;
  status: "proposed" | "accepted" | "dismissed";
  source: "feedback" | "manual";
  feedback_id: string | null;
  at: string;
};
export const listLessons = (status = "", agentId = "") => skillCall<{ lessons: AgentLesson[] }>(`/api/quality/lessons${qs({ status, agent_id: agentId })}`, {}, "Couldn't load lessons");
export const acceptLesson = (id: string, text = "") =>
  skillCall<AgentLesson>(`/api/quality/lessons/${id}/accept`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }) }, "Couldn't accept the lesson");
export const dismissLesson = (id: string) => skillCall<{ ok: boolean }>(`/api/quality/lessons/${id}/dismiss`, { method: "POST" }, "Couldn't dismiss the lesson");
export const removeLesson = (id: string) => skillCall<{ ok: boolean }>(`/api/quality/lessons/${id}`, { method: "DELETE" }, "Couldn't remove the lesson");
export const addLesson = (agentId: string, text: string) =>
  skillCall<AgentLesson>("/api/quality/lessons", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ agent_id: agentId, text }) }, "Couldn't add the lesson");

// Watches: agents wake when a page changes or the inbox fills up (apps/api/app/automations/watches.py).
export type Watch = {
  id: number;
  name: string;
  trigger: string;
  trigger_type: "page" | "inbox";
  condition: string;
  action: string;
  enabled: boolean;
  agent_id: string | null;
  watch: {
    every_minutes: number | null;
    checked_at: string | null;
    changed_at: string | null;
    fired: number;
    error: string;
    title: string;
    count: number | null;
    last_change: { added: string[]; removed: string[] } | null;
    baseline: boolean | null;
  };
};
export async function listWatches(agentId?: string | null) {
  const data = await skillCall<{ automations: (Watch & { trigger_type: string })[] }>(`/api/automations${qs({ agent_id: agentId })}`, {}, "Couldn't load watches");
  return data.automations.filter((a): a is Watch => a.trigger_type === "page" || a.trigger_type === "inbox");
}
export const createWatch = (body: { trigger_type: "page" | "inbox"; name: string; trigger: string; action: string; condition?: string; every_minutes?: number; agent_id?: string | null }) =>
  skillCall<Watch>("/api/automations/watches", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }, "Couldn't create the watch");
export const checkWatch = (id: number) => skillCall<Watch>(`/api/automations/${id}/check`, { method: "POST" }, "Couldn't check it");
export const setWatchEnabled = (id: number, enabled: boolean) =>
  skillCall<Watch>(`/api/automations/${id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled }) }, "Couldn't change it");
export const deleteWatch = (id: number) => skillCall<{ ok: boolean }>(`/api/automations/${id}`, { method: "DELETE" }, "Couldn't delete it");

// ---- One approvals queue (docs/APPROVALS.md) ----
export type ApprovalKind = "plan" | "action" | "question" | "handoff" | "chat";
export type ApprovalItem = {
  id: string;
  kind: ApprovalKind;
  title: string;
  lines: string[];
  agent_id: string | null;
  agent_name: string;
  agent_color: string;
  agent_avatar: string;
  session_id: string | null;
  job_id: string | null;
  job_title?: string;
  token?: string;
  message_id?: number;
  options?: string[];
  since: string | null;
  expires_at?: number | null;
  expired?: boolean;
};
export type ApprovalQueue = { items: ApprovalItem[]; count: number; counts: Partial<Record<ApprovalKind, number>> };
const JSON_POST = (body: unknown) => ({ method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
export const listApprovals = () => skillCall<ApprovalQueue>("/api/approvals", {}, "Couldn't load approvals");
export const approvalsCount = () => skillCall<{ count: number }>("/api/approvals/count", {}, "");
export const decideApprovals = (decisions: { id: string; kind: ApprovalKind; job_id: string; token?: string; approve: boolean }[]) =>
  skillCall<{ results: { id: string; ok: boolean; error?: string; status?: string }[]; done: number }>("/api/approvals/decide", JSON_POST({ decisions }), "Couldn't apply that");
export const answerApproval = (job_id: string, text: string) => skillCall("/api/approvals/answer", JSON_POST({ job_id, text }), "Couldn't send the answer");
export const dismissChatApproval = (message_id: number, token: string) => skillCall("/api/approvals/dismiss", JSON_POST({ message_id, token }), "Couldn't decline it");
/** Approve a chat approval the same way the chat does: a plain "Yes" with its token, sent into that chat. */
export async function approveChatApproval(item: ApprovalItem) {
  const res = await fetch("/api/chat", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message: "Yes", mode: "interrupt", session_id: item.session_id, agent_id: item.agent_id, confirmations: [{ token: item.token }] }),
  });
  const raw = (await res.text()).trim();
  let data: any = {};
  try {
    data = raw ? JSON.parse(raw) : {};
  } catch {
    /* heartbeat-only or non-JSON */
  }
  if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Couldn't approve it");
  return String(data.text || "");
}
