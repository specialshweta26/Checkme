"use client";

import { FormEvent, useEffect, useRef, useState } from "react";
import { AttachmentWidget } from "../AttachmentWidget";
import { AppShell } from "../components/AppShell";
import { chatAttachments, ComputerIcon, computerFromMeta, ScreenRail } from "../components/ComputerPanel";
import { cardsFromMeta, ConnectCards } from "../components/ConnectCard";
import { EmailThreadButton, mailRefsFromMeta } from "../components/EmailThread";
import { Markdown } from "../components/Markdown";
import { dismissFocusCard, focusCardDismissed, FocusOnboarding, type FocusOption } from "../components/FocusCard";
import { JobTimeline } from "../components/JobTimeline";
import { formatPreview } from "../components/preview";
import { Thinking } from "../components/Thinking";
import { announceWorkspaceChanged, chatHref, updateAgent, deleteSession, getMe, listSessions, loadSession, rememberScope, uploadFiles, startTeaching, stopTeaching, type ChatSessionRow, type Me, type UploadedFile, type WorkspaceRoster } from "../lib";

const STORE = "assistant.session";

function scopeKey(agentId?: string | null, groupId?: string | null) {
  if (groupId) return `g.${groupId}`;
  if (agentId) return `a.${agentId}`;
  return "";
}

function scopeStore(agentId?: string | null, groupId?: string | null) {
  const key = scopeKey(agentId, groupId);
  return key ? `${STORE}.${key}` : STORE;
}

function newProgressId() {
  const rand = Math.random().toString(36).slice(2, 12);
  return `turn_${Date.now().toString(36)}${rand}`;
}

/** Tools that drive the computer: when one runs, the live screen opens so the user can watch. */
const COMPUTER_TOOL = /^(browser_|browse_|computer_|launch_app$|open_path$|request_takeover$|web_fetch$)/;

function usesComputer(tools: string[]) {
  return tools.some((name) => COMPUTER_TOOL.test(name));
}

/** Poll the API for live status lines of one in-flight chat turn. Returns a stop function. */
function pollProgress(
  progressId: string,
  onSteps: (steps: string[]) => void,
  everyMs = 1200,
  onDone?: () => void,
  onTools?: (tools: string[]) => void,
) {
  let stopped = false;
  let last = 0;
  let timer: number | undefined;
  const tick = async () => {
    if (stopped) return;
    try {
      const res = await fetch(`/api/chat/progress/${progressId}`, { credentials: "include", cache: "no-store" });
      if (res.ok) {
        const data = await res.json();
        const steps: string[] = (data.steps || []).map((s: any) => String(s?.text || "")).filter(Boolean);
        if (!stopped && steps.length !== last) {
          last = steps.length;
          onSteps(steps);
          const tools: string[] = (data.steps || []).map((s: any) => String(s?.tool || "")).filter(Boolean);
          if (tools.length) onTools?.(tools);
        }
        if (data.done) {
          onDone?.();
          return;
        }
      }
    } catch {
      // Status is best effort; the chat reply still arrives on the POST.
    }
    if (!stopped) timer = window.setTimeout(tick, everyMs);
  };
  timer = window.setTimeout(tick, 600);
  return () => {
    stopped = true;
    if (timer) window.clearTimeout(timer);
  };
}

/** True when the chat POST died in transit (Safari "Load failed", proxy 502/504, cut stream), not on the server. */
function lostInTransit(err: any) {
  return err instanceof TypeError || err?.lostInTransit === true;
}

function transitError(message: string) {
  const err: any = new Error(message);
  err.lostInTransit = true;
  return err;
}

/**
 * The POST for a turn was lost, but the turn keeps running on the server. Follow its progress id until it is
 * done, then load the stored session (retrying while the reply is still being saved). Returns null if the turn
 * could not be found in time.
 */
async function recoverTurn(progressId: string, knownSessionId: string | null, maxMs = 15 * 60 * 1000) {
  const started = Date.now();
  let sessionId = knownSessionId;
  let done = false;
  while (Date.now() - started < maxMs) {
    try {
      const res = await fetch(`/api/chat/progress/${progressId}`, { credentials: "include", cache: "no-store" });
      if (res.ok) {
        const data = await res.json();
        if (data.session_id) sessionId = String(data.session_id);
        if (data.done) done = true;
      }
    } catch {
      // Still offline; keep waiting.
    }
    if (done && sessionId) {
      const loaded = await loadSession(sessionId).catch(() => null);
      const last = loaded?.messages?.filter((m: any) => m.role === "assistant").slice(-1)[0];
      if (loaded?.messages && !last?.meta?.in_progress) return { sessionId, loaded };
    }
    await new Promise((r) => window.setTimeout(r, done ? 1500 : 3000));
  }
  return null;
}

function sessionInScopeRow(row: ChatSessionRow | undefined, agentId?: string | null, groupId?: string | null) {
  if (!row) return false;
  if (groupId) return row.group_id === groupId;
  return !row.group_id && !!agentId && row.agent_id === agentId;
}

type Msg = { role: "user" | "assistant"; content: string; meta?: any };
type Confirm = { token: string; title: string; preview: any; kind?: string };
type AgentView = { sessionId: string | null; messages: Msg[]; pending: Confirm[] };

function pendingFor(messages: Msg[]): Confirm[] {
  const latest = [...messages].reverse().find((m) => m.role === "assistant" && Array.isArray(m.meta?.pending_confirms));
  return (latest?.meta?.pending_confirms || []) as Confirm[];
}

export default function ChatPage() {
  const [me, setMe] = useState<Me | null>(null);
  const [messages, setMessages] = useState<Msg[]>([]);
  const [text, setText] = useState("");
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [sessions, setSessions] = useState<ChatSessionRow[]>([]);
  const [pending, setPending] = useState<Confirm[]>([]);
  const [busyScopes, setBusyScopes] = useState<string[]>([]);
  const [progress, setProgress] = useState<Record<string, { steps: string[]; hint: string }>>({});
  const viewsRef = useRef<Record<string, AgentView>>({});
  const scopeRef = useRef("");
  const inflightRef = useRef<Set<string>>(new Set());
  const progressStopsRef = useRef<Record<string, () => void>>({});
  const [notice, setNotice] = useState("");
  const end = useRef<HTMLDivElement>(null);
  const box = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const [uploads, setUploads] = useState<UploadedFile[]>([]);
  const [uploadError, setUploadError] = useState("");
  const [watching, setWatching] = useState(false);
  // True once the running turn has called a computer tool (so the screen rail may boot the desktop).
  const [computerTurn, setComputerTurn] = useState(false);
  const [controlling, setControlling] = useState(false);
  const [teaching, setTeaching] = useState(false);
  const [screenOpen, setScreenOpen] = useState(false);
  const [agentId, setAgentId] = useState<string | null>(null);
  const [groupId, setGroupId] = useState<string | null>(null);
  const [roster, setRoster] = useState<WorkspaceRoster>({ agents: [], groups: [] });
  const [hello, setHello] = useState("Hello");
  const [focusHidden, setFocusHidden] = useState<Record<string, boolean>>({});

  async function refreshSessions() {
    setSessions(await listSessions());
  }

  function persistView(key: string, extra?: Partial<AgentView>) {
    if (!key) return;
    const prev = viewsRef.current[key];
    if (inflightRef.current.has(key) && prev && !extra) return;
    viewsRef.current[key] = {
      sessionId,
      messages,
      pending,
      ...extra,
    };
  }

  function showView(view: AgentView) {
    setSessionId(view.sessionId);
    setMessages(view.messages);
    setPending(view.pending);
  }

  async function openSession(id: string) {
    const data = await loadSession(id);
    if (!data) return;
    const sess = data.session || {};
    const nextAgent = sess.group_id ? null : sess.agent_id || roster.default_agent_id || null;
    const nextGroup = sess.group_id || null;
    const nextMessages = (data.messages || [])
      .filter((m: Msg) => m.role === "user" || m.role === "assistant")
      .map((m: Msg) => ({ role: m.role, content: m.content || "", meta: m.meta }));
    const view = { sessionId: id, messages: nextMessages, pending: pendingFor(nextMessages) };
    viewsRef.current[scopeKey(nextAgent, nextGroup)] = view;
    showView(view);
    setGroupId(nextGroup);
    setAgentId(nextAgent);
    rememberScope(nextAgent, nextGroup);
    window.localStorage.setItem(scopeStore(nextAgent, nextGroup), id);
    if (typeof window !== "undefined") {
      window.history.replaceState(null, "", chatHref({ agent: nextAgent, group: nextGroup, conversation: id }));
    }
    resumeProgress(scopeKey(nextAgent, nextGroup), id, nextMessages);
  }

  function newChat() {
    const key = scopeKey(agentId, groupId);
    setSessionId(null);
    setMessages([]);
    setPending([]);
    setNotice("");
    setUploads([]);
    setWatching(false);
    setComputerTurn(false);
    setControlling(false);
    setScreenOpen(false);
    if (key && !inflightRef.current.has(key)) {
      viewsRef.current[key] = { sessionId: null, messages: [], pending: [] };
    }
    window.localStorage.removeItem(scopeStore(agentId, groupId));
    box.current?.focus();
  }

  useEffect(() => {
    setHello(greeting(me));
  }, [me]);

  useEffect(() => {
    getMe().then(async (u) => {
      if (!u) {
        window.location.replace("/");
        return;
      }
      if (!u.timezone || u.timezone === "UTC") {
        await fetch("/api/me", {
          method: "PATCH",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ timezone: "Asia/Kolkata" }),
        }).catch(() => undefined);
        u = { ...u, timezone: "Asia/Kolkata" };
      }
      setMe(u);
      setSessions(await listSessions());
    });
    const q = new URLSearchParams(window.location.search);
    if (q.get("mcp") === "connected") setNotice("MCP connected. You can use it in this chat.");
    if (q.get("error")) setNotice(q.get("error")?.replaceAll("_", " ") || "");
    if (q.get("agent")) setAgentId(q.get("agent"));
    if (q.get("group")) {
      setGroupId(q.get("group"));
      setAgentId(null);
    }
    if (q.get("new") === "1") {
      setSessionId(null);
      setMessages([]);
      setPending([]);
      window.history.replaceState(null, "", chatHref({ agent: q.get("agent"), group: q.get("group") }));
    }
  }, []);

  useEffect(() => {
    const cards = document.querySelectorAll(".connect-card");
    const card = cards[cards.length - 1];
    if (card) {
      card.scrollIntoView({ behavior: "smooth", block: "center" });
      return;
    }
    end.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, pending, busyScopes]);

  async function send(message: string, confirmations: any[] = []) {
    const key = scopeKey(agentId, groupId);
    if ((!message.trim() && !uploads.length) || !key || busyScopes.includes(key)) return;
    const sendAgent = agentId;
    const sendGroup = groupId;
    const pendingFiles = uploads;
    const userTurn: Msg = { role: "user", content: message || "Attached files", meta: pendingFiles.length ? { uploads: pendingFiles } : undefined };
    const nextMessages = [...messages, userTurn];
    setWatching(true);
    inflightRef.current.add(key);
    setBusyScopes((keys) => (keys.includes(key) ? keys : [...keys, key]));
    setMessages(nextMessages);
    persistView(key, { messages: nextMessages, pending: [] });
    setText("");
    setUploads([]);
    setUploadError("");
    const progressId = newProgressId();
    setProgress((all) => ({ ...all, [key]: { steps: [], hint: message || "your files" } }));
    let screenShown = false;
    const stopProgress = pollProgress(
      progressId,
      (steps) => setProgress((all) => (all[key] ? { ...all, [key]: { ...all[key], steps } } : all)),
      1200,
      undefined,
      (tools) => {
        // Open the live computer once per turn when the agent starts using it (the user can close it again).
        if (screenShown || scopeRef.current !== key || !usesComputer(tools)) return;
        screenShown = true;
        setComputerTurn(true);
        setScreenOpen(true);
      },
    );
    progressStopsRef.current[key] = stopProgress;
    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message,
          session_id: sessionInScopeRow(sessions.find((s) => s.id === sessionId), sendAgent, sendGroup) ? sessionId : null,
          confirmations,
          attachments: pendingFiles,
          agent_id: sendGroup ? null : sendAgent,
          group_id: sendGroup,
          progress_id: progressId,
        }),
      });
      // Long turns stream whitespace heartbeats before the JSON reply; JSON.parse ignores leading spaces.
      const raw = (await res.text()).trim();
      let data: any = {};
      try {
        data = raw ? JSON.parse(raw) : {};
      } catch {
        if (res.status >= 500 || !raw) throw transitError(raw.slice(0, 280) || "Chat failed");
        throw new Error(raw.slice(0, 280) || "Chat failed");
      }
      if (!raw) throw transitError("Empty reply");
      if (!res.ok || data.stream_error) {
        const detail = typeof data.detail === "string" ? data.detail : data.detail?.[0]?.msg;
        throw new Error(detail || raw.slice(0, 280) || "Chat failed");
      }
      if (data.workspace_changed) announceWorkspaceChanged();
      const loaded = data.session_id ? await loadSession(data.session_id).catch(() => null) : null;
      const peerTurns: Msg[] = (data.thread || [])
        .filter((item: any) => (item?.text || "").trim())
        .map((item: any) => ({
          role: "assistant" as const,
          content: item.text || "",
          meta: {
            agent_id: item.agent_id,
            agent_name: item.agent_name,
            agent_color: item.agent_color,
            peer: true,
            used_tools: item.used_tools,
            computer: item.computer,
            attachments: item.attachments,
          },
        }));
      const ownerTurn: Msg = {
        role: "assistant",
        content: data.text || "",
        meta: { ...data, connect_cards: cardsFromMeta(data) },
      };
      const doneMessages: Msg[] = loaded?.messages?.length
        ? loaded.messages
            .filter((m: Msg) => m.role === "user" || m.role === "assistant")
            .map((m: Msg) => ({ role: m.role, content: m.content || "", meta: m.meta }))
        : [...nextMessages, ownerTurn, ...peerTurns];
      const nextPending: Confirm[] = data.pending_confirms || [];
      const doneView: AgentView = { sessionId: data.session_id || sessionId, messages: doneMessages, pending: nextPending };
      viewsRef.current[key] = doneView;
      if (data.session_id) window.localStorage.setItem(scopeStore(sendAgent, sendGroup), data.session_id);
      if (scopeRef.current === key) {
        showView(doneView);
        if (nextPending.some((p) => p.kind === "handoff" || p.preview?.kind === "handoff")) {
          setControlling(true);
          setScreenOpen(true);
        }
      }
      await refreshSessions();
    } catch (err: any) {
      // The connection dropped (e.g. mobile Safari's "Load failed"), but the server keeps working on the turn.
      // Follow it by progress id and show the stored reply instead of the raw network error.
      const inScope = sessionInScopeRow(sessions.find((s) => s.id === sessionId), sendAgent, sendGroup) ? sessionId : null;
      const recovered = lostInTransit(err) ? await recoverTurn(progressId, inScope) : null;
      if (recovered) {
        const msgs: Msg[] = recovered.loaded.messages
          .filter((m: Msg) => m.role === "user" || m.role === "assistant")
          .map((m: Msg) => ({ role: m.role, content: m.content || "", meta: m.meta }));
        const view: AgentView = { sessionId: recovered.sessionId, messages: msgs, pending: pendingFor(msgs) };
        viewsRef.current[key] = view;
        window.localStorage.setItem(scopeStore(sendAgent, sendGroup), recovered.sessionId);
        if (scopeRef.current === key) showView(view);
        await refreshSessions().catch(() => null);
      } else {
        const content = lostInTransit(err)
          ? "The connection dropped before the reply arrived. The task may still finish; reopen this chat in a moment to see it."
          : err.message || "Something went wrong";
        const failView: AgentView = {
          sessionId,
          messages: [...nextMessages, { role: "assistant", content }],
          pending: [],
        };
        viewsRef.current[key] = failView;
        if (scopeRef.current === key) showView(failView);
      }
    } finally {
      stopProgress();
      if (progressStopsRef.current[key] === stopProgress) delete progressStopsRef.current[key];
      setProgress((all) => {
        const next = { ...all };
        delete next[key];
        return next;
      });
      inflightRef.current.delete(key);
      setBusyScopes((keys) => keys.filter((k) => k !== key));
      if (scopeRef.current === key) {
        setWatching(false);
        setComputerTurn(false);
      }
    }
  }

  async function reloadSession(id: string | null) {
    // A background job finished: pull its final reply into the open chat.
    if (!id) return;
    const loaded = await loadSession(id).catch(() => null);
    if (!loaded?.messages) return;
    const next: Msg[] = loaded.messages
      .filter((m: Msg) => m.role === "user" || m.role === "assistant")
      .map((m: Msg) => ({ role: m.role, content: m.content || "", meta: m.meta }));
    const key = scopeKey(loaded.session?.agent_id, loaded.session?.group_id);
    const view = { sessionId: id, messages: next, pending: pendingFor(next) };
    viewsRef.current[key] = view;
    if (!next.some((m) => m.meta?.in_progress) && progressStopsRef.current[key]) {
      progressStopsRef.current[key]();
      delete progressStopsRef.current[key];
      inflightRef.current.delete(key);
      setBusyScopes((keys) => keys.filter((item) => item !== key));
      if (scopeRef.current === key) {
        setWatching(false);
        setComputerTurn(false);
      }
    }
    if (scopeRef.current === key) showView(view);
  }

  function resumeProgress(key: string, id: string, loadedMessages: Msg[]) {
    const active = [...loadedMessages].reverse().find((m) => m.role === "assistant" && m.meta?.in_progress && m.meta?.progress_id);
    if (!active || progressStopsRef.current[key]) return;
    const progressId = String(active.meta.progress_id);
    const hint = [...loadedMessages].reverse().find((m) => m.role === "user")?.content || "your request";
    inflightRef.current.add(key);
    setBusyScopes((keys) => (keys.includes(key) ? keys : [...keys, key]));
    setProgress((all) => ({ ...all, [key]: { steps: [], hint } }));
    if (scopeRef.current === key) setWatching(true);
    const stop = pollProgress(
      progressId,
      (steps) => setProgress((all) => (all[key] ? { ...all, [key]: { ...all[key], steps } } : all)),
      1200,
      () => {
        window.setTimeout(() => void reloadSession(id), 350);
      },
    );
    progressStopsRef.current[key] = stop;
  }

  async function removeChat(id: string) {
    await deleteSession(id);
    if (sessionId === id) newChat();
    await refreshSessions();
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    send(text);
  }

  const currentKey = scopeKey(agentId, groupId);
  scopeRef.current = currentKey;
  const busyHere = busyScopes.includes(currentKey);

  const activeAgent = roster.agents.find((a) => a.id === agentId) || roster.agents[0];
  const activeGroup = roster.groups.find((g) => g.id === groupId) || null;
  const screenName = activeGroup?.name || activeAgent?.name || "Chief";
  const screenColor = activeGroup?.members[0]?.color || activeAgent?.color || "#f97316";
  const ownerName = activeGroup?.owner_id
    ? activeGroup.members.find((m) => m.id === activeGroup.owner_id)?.name
    : activeGroup?.members[0]?.name;
  const screenMeta = activeGroup
    ? [activeGroup.outcome, activeGroup.members.map((m) => m.name).join(" · "), ownerName ? `Owner: ${ownerName}` : ""]
        .filter(Boolean)
        .join(" · ")
    : activeAgent?.title || "";
  const showFocusCard = Boolean(
    !activeGroup &&
      activeAgent &&
      (activeAgent.kind || "custom") !== "default" &&
      !activeAgent.focus &&
      !focusHidden[activeAgent.id] &&
      !focusCardDismissed(activeAgent.id),
  );

  function hideFocusCard(id: string) {
    dismissFocusCard(id);
    setFocusHidden((all) => ({ ...all, [id]: true }));
  }

  async function pickFocus(option: FocusOption) {
    if (!activeAgent) return;
    const id = activeAgent.id;
    try {
      const updated = await updateAgent(id, { focus: option.key });
      setRoster((r) => ({ ...r, agents: r.agents.map((a) => (a.id === id ? updated : a)) }));
      announceWorkspaceChanged();
    } catch {
      // The agent can still set its own focus from the answer (set_my_focus).
    }
    hideFocusCard(id);
  }
  const scopedSessions = sessions.filter((s) => sessionInScopeRow(s, agentId, groupId));
  const computer = [...messages].reverse().map((m) => computerFromMeta(m.meta)).find(Boolean) || null;

  function selectAgent(id: string) {
    persistView(scopeKey(agentId, groupId));
    setControlling(false);
    setWatching(false);
    setComputerTurn(false);
    setAgentId(id);
    setGroupId(null);
    rememberScope(id, null);
    if (typeof window !== "undefined") window.history.replaceState(null, "", chatHref({ agent: id }));
    const next = scopeKey(id, null);
    scopeRef.current = next;
    const cached = viewsRef.current[next];
    if (cached) {
      showView(cached);
      return;
    }
    showView({ sessionId: null, messages: [], pending: [] });
    void restoreScope(id, null, sessions, roster);
  }

  function selectGroup(id: string) {
    persistView(scopeKey(agentId, groupId));
    setControlling(false);
    setWatching(false);
    setComputerTurn(false);
    setGroupId(id);
    setAgentId(null);
    rememberScope(null, id);
    if (typeof window !== "undefined") window.history.replaceState(null, "", chatHref({ group: id }));
    const next = scopeKey(null, id);
    scopeRef.current = next;
    const cached = viewsRef.current[next];
    if (cached) {
      showView(cached);
      return;
    }
    showView({ sessionId: null, messages: [], pending: [] });
    void restoreScope(null, id, sessions, roster);
  }

  async function restoreScope(nextAgent: string | null, nextGroup: string | null, rows: ChatSessionRow[], nextRoster: WorkspaceRoster) {
    const wanted = scopeKey(nextAgent, nextGroup);
    if (inflightRef.current.has(wanted) && viewsRef.current[wanted]) {
      if (scopeRef.current === wanted) showView(viewsRef.current[wanted]);
      return;
    }
    const scoped = rows.filter((s) => sessionInScopeRow(s, nextAgent, nextGroup));
    const stored = typeof window !== "undefined" ? window.localStorage.getItem(scopeStore(nextAgent, nextGroup)) : null;
    const hinted = nextGroup
      ? nextRoster.groups.find((g) => g.id === nextGroup)?.session_id
      : nextRoster.agents.find((a) => a.id === nextAgent)?.session_id;
    const pick = (stored && scoped.some((s) => s.id === stored) ? stored : null) || scoped[0]?.id || hinted || null;
    const apply = (view: AgentView) => {
      viewsRef.current[wanted] = view;
      if (scopeRef.current === wanted) showView(view);
    };
    if (pick) {
      const data = await loadSession(pick);
      if (!data) {
        apply({ sessionId: null, messages: [], pending: [] });
        return;
      }
      const nextMessages = (data.messages || [])
        .filter((m: Msg) => m.role === "user" || m.role === "assistant")
        .map((m: Msg) => ({ role: m.role, content: m.content || "", meta: m.meta }));
      apply({ sessionId: pick, messages: nextMessages, pending: pendingFor(nextMessages) });
      resumeProgress(wanted, pick, nextMessages);
      return;
    }
    apply({ sessionId: null, messages: [], pending: [] });
  }

  return (
    <AppShell
      me={me}
      current="chat"
      conversations={scopedSessions}
      threads={sessions}
      activeId={sessionId}
      activeAgentId={agentId}
      activeGroupId={groupId}
      busyScopes={busyScopes}
      headline={screenName}
      headlineMeta={screenMeta}
      headlineColor={screenColor}
      computerOpen={screenOpen}
      computerLive={Boolean(watching || controlling || computer?.live)}
      onToggleComputer={() => setScreenOpen((open) => !open)}
      onNewChat={newChat}
      onOpenChat={openSession}
      onDeleteChat={removeChat}
      onSelectAgent={selectAgent}
      onSelectGroup={selectGroup}
      onConversationCleared={(id) => {
        const key = scopeKey(id, null);
        viewsRef.current[key] = { sessionId: null, messages: [], pending: [] };
        window.localStorage.removeItem(scopeStore(id, null));
        if (scopeRef.current === key) {
          setSessionId(null);
          setMessages([]);
          setPending([]);
        }
        void refreshSessions();
      }}
      onRoster={(next) => {
        setRoster(next);
        const q = typeof window !== "undefined" ? new URLSearchParams(window.location.search) : null;
        const wantGroup = q?.get("group");
        const wantAgent = q?.get("agent");
        if (wantGroup && next.groups.some((g) => g.id === wantGroup)) {
          if (groupId !== wantGroup) {
            setGroupId(wantGroup);
            setAgentId(null);
            rememberScope(null, wantGroup);
            void restoreScope(null, wantGroup, sessions, next);
          }
          return;
        }
        if (wantAgent && next.agents.some((a) => a.id === wantAgent)) {
          if (agentId !== wantAgent || groupId) {
            setGroupId(null);
            setAgentId(wantAgent);
            rememberScope(wantAgent, null);
            void restoreScope(wantAgent, null, sessions, next);
          }
          return;
        }
        if (agentId || groupId) return;
        const aid = next.default_agent_id || next.agents[0]?.id || null;
        setAgentId(aid);
        if (aid) {
          rememberScope(aid, null);
          void restoreScope(aid, null, sessions, next);
        }
      }}
    >
      <div className="workspace">
      <div className="chat">
        <div className="chat-head">
          {activeGroup ? (
            <span className="group-stack chat-head-face" aria-hidden>
              {activeGroup.members.slice(0, 3).map((m) => (
                <span key={m.id} className="agent-avatar tiny" style={{ background: m.color }}>
                  {(m.name || "?").slice(0, 1).toUpperCase()}
                </span>
              ))}
            </span>
          ) : (
            <span className="agent-avatar chat-head-face" style={{ background: screenColor }}>
              {screenName.slice(0, 1).toUpperCase()}
            </span>
          )}
          <div className="chat-head-copy">
            <strong>{screenName}</strong>
            {screenMeta ? <span className="muted">{screenMeta}</span> : null}
          </div>
          <button
            type="button"
            className={`chat-computer-btn${screenOpen ? " on" : ""}${watching || controlling || computer?.live ? " live" : ""}`}
            aria-label={screenOpen ? "Hide computer" : "Open computer"}
            aria-pressed={screenOpen}
            onClick={() => setScreenOpen((open) => !open)}
          >
            <ComputerIcon />
          </button>
        </div>
        <div className="transcript">
          <div className="thread">
            {notice ? <p className="notice">{notice}</p> : null}
            {messages.length === 0 && !busyHere && showFocusCard && activeAgent ? (
              <FocusOnboarding
                userName={firstName(me)}
                agentName={activeAgent.name}
                agentColor={activeAgent.color}
                disabled={busyHere}
                onPick={(opt) => void pickFocus(opt)}
                onDismiss={() => hideFocusCard(activeAgent.id)}
              />
            ) : null}
            {messages.length === 0 && !busyHere ? (
              <div className={showFocusCard ? "welcome welcome-after-onboard" : "welcome"}>
                {showFocusCard ? null : (
                  <>
                    <h1>{hello}</h1>
                    <p>
                      {activeGroup
                        ? `${firstName(me)} set this group up. The kickoff owner introduces proposed roles, then each bot replies as itself. @ one bot, or @everyone, and they talk to each other in this thread.`
                        : activeAgent?.suggested_tasks?.length
                          ? `${activeAgent.name} is ready to work. Pick a useful first task, or describe a different one below.`
                          : "Here's what's happening today — brief the inbox, ask a connected system, or schedule a send."}
                    </p>
                  </>
                )}
                <div className="hero-cards">
                  {activeGroup ? (
                    <>
                      {activeGroup.outcome ? (
                        <button className="hero-card" onClick={() => send(activeGroup.outcome || "")}>
                          <span className="mark" />
                          <strong>Start the group’s outcome</strong>
                          <span>{activeGroup.outcome} The kickoff owner can hand work to teammates and coordinate their replies here.</span>
                        </button>
                      ) : null}
                      <button
                        className="hero-card"
                        onClick={() =>
                          send("Hi team — please each reply with a one-line hello and what you're best at, so we know the group works.")
                        }
                      >
                        <span className="mark" />
                        <strong>Introduce the team</strong>
                        <span>Kickoff owner names who created the group and proposed roles. Each bot then says hello as itself.</span>
                      </button>
                      <button
                        className="hero-card"
                        onClick={() => send("@everyone Status from each of you — what you own and what's next.")}
                      >
                        <span className="mark" />
                        <strong>Ask everyone</strong>
                        <span>Each responsible bot replies in its own bubble. They can hand work to each other.</span>
                      </button>
                    </>
                  ) : activeAgent?.suggested_tasks?.length ? (
                    activeAgent.suggested_tasks.map((task) => (
                      <button key={task.title} className="hero-card" onClick={() => send(task.prompt)}>
                        <span className="mark" />
                        <strong>{task.title}</strong>
                        <span>{task.prompt}</span>
                      </button>
                    ))
                  ) : (
                    <>
                      <button className="hero-card" onClick={() => send("What matters today? Give me my chief of staff briefing.")}>
                        <span className="mark" />
                        <strong>Summarize emails</strong>
                        <span>Inbox, calendar, and the decisions that cannot wait.</span>
                      </button>
                      <button className="hero-card" onClick={() => send("What am I waiting for, and what did I promise?")}>
                        <span className="mark" />
                        <strong>Check connected systems</strong>
                        <span>Promises, waits, or any tool already linked to this workspace.</span>
                      </button>
                      <a className="hero-card" href="/tasks">
                        <span className="mark" />
                        <strong>Schedule a send</strong>
                        <span>Work that should run at a time you choose, then go out.</span>
                      </a>
                    </>
                  )}
                </div>
              </div>
            ) : null}
            {messages.map((m, i) =>
              m.role === "user" ? (
                <div key={i} className="turn user">
                  <div className="bubble">{m.content}</div>
                  {m.meta?.uploads?.length ? <AttachmentWidget items={m.meta.uploads} /> : null}
                </div>
              ) : (
                <div key={i} className={`turn assistant${m.meta?.agent_name ? " speaker" : ""}`}>
                  {m.meta?.agent_name ? (
                    <div className="agent-chip">
                      <span className="agent-avatar tiny" style={{ background: m.meta.agent_color || "#f97316" }}>
                        {String(m.meta.agent_name).slice(0, 1)}
                      </span>
                      {m.meta.agent_name}
                    </div>
                  ) : null}
                  {m.meta?.agent_name ? (
                    <div className="group-bubble">
                      <Markdown text={m.content} />
                    </div>
                  ) : (
                    <Markdown text={m.content} />
                  )}
                  <ConnectCards
                    meta={m.meta}
                    agentId={m.meta?.agent_id || agentId}
                    agentName={m.meta?.agent_name}
                  />
                  {m.meta?.task_state?.skill_name ? (
                    <div className="skill-used" title={(m.meta.task_state.notes || []).map((n: any) => n.text).join("\n")}>
                      Skill: {m.meta.task_state.skill_name}
                      {m.meta.task_state.step_count
                        ? ` · ${Math.min(m.meta.task_state.step_index || 0, m.meta.task_state.step_count)}/${m.meta.task_state.step_count} steps`
                        : ""}
                      {m.meta.task_state.status === "handed_off" ? " · handed over" : m.meta.task_state.status === "done" ? " · done" : ""}
                    </div>
                  ) : null}
                  {m.meta?.job?.id && !m.meta?.job_result ? (
                    <JobTimeline
                      jobId={String(m.meta.job.id)}
                      after={Number(m.meta.job_after) || 0}
                      onSettled={() => reloadSession(sessionId)}
                    />
                  ) : null}
                  {m.meta?.choices?.options?.length && i === messages.length - 1 ? (
                    <div className="chips choices" style={{ marginTop: 8 }}>
                      {m.meta.choices.options.map((opt: string) => (
                        <button
                          key={opt}
                          className="chip"
                          disabled={busyHere}
                          onClick={async () => {
                            if (m.meta.choices.job_id) {
                              // A background job asked: answer it directly, then show its timeline again.
                              await fetch(`/api/jobs/${m.meta.choices.job_id}/answer`, {
                                method: "POST",
                                credentials: "include",
                                headers: { "Content-Type": "application/json" },
                                body: JSON.stringify({ text: opt }),
                              }).catch(() => null);
                              reloadSession(sessionId);
                            } else {
                              send(opt);
                            }
                          }}
                        >
                          {opt}
                        </button>
                      ))}
                    </div>
                  ) : null}
                  {sourceLabel(m.meta) ? <div className="source-chip">{sourceLabel(m.meta)}</div> : null}
                  <EmailThreadButton refs={mailRefsFromMeta(m.meta)} />
                  {chatAttachments(m.meta?.attachments).length ? <AttachmentWidget items={chatAttachments(m.meta.attachments)} /> : null}
                  {m.meta?.clarify?.length ? (
                    <div className="chips" style={{ marginTop: 8 }}>
                      {m.meta.clarify.map((c: any) => (
                        <button key={c.id} className="chip" onClick={() => send(`Use @${c.slug || c.name} for that.`)}>
                          {c.name}
                        </button>
                      ))}
                    </div>
                  ) : null}
                </div>
              ),
            )}
            {busyHere ? <Thinking steps={progress[currentKey]?.steps} hint={progress[currentKey]?.hint} /> : null}
            <div ref={end} />
          </div>
        </div>
        <div className="chat-footer">
        {pending.length ? (
          <div className="approval">
            {pending.map((p) => (
              <div key={p.token}>
                <h3>{p.title || "Approve this action"}</h3>
                <p className="muted" style={{ margin: "0 0 8px" }}>
                  {p.kind === "handoff" || p.preview?.kind === "handoff"
                    ? "Take control on the computer screen, finish the step, then continue."
                    : p.kind === "git_push" || p.preview?.repo
                      ? "Review the repo, branch and commits, then approve. Nothing is pushed, opened or merged until you do."
                      : "Review the recipient and preview, then confirm. Nothing sends until you do."}
                </p>
                <pre className="approval-preview">{formatPreview(p.preview)}</pre>
                <div className="approval-actions">
                  {p.kind === "handoff" || p.preview?.kind === "handoff" ? (
                    <button className="btn" type="button" onClick={() => { setControlling(true); setScreenOpen(true); }}>
                      Take control
                    </button>
                  ) : null}
                  <button
                    className="btn"
                    disabled={busyHere}
                    onClick={() => {
                      setPending([]);
                      setControlling(false);
                      send(
                        p.kind === "handoff" || p.preview?.kind === "handoff"
                          ? "I finished on the computer. Continue."
                          : "Confirmed. Continue.",
                        pending,
                      );
                    }}
                  >
                    {p.kind === "handoff" || p.preview?.kind === "handoff" ? "Continue" : "Confirm"}
                  </button>
                  <button className="btn secondary" onClick={() => setPending([])}>
                    Cancel
                  </button>
                </div>
              </div>
            ))}
          </div>
        ) : null}
        <div className="composer-wrap">
          {uploadError ? <p className="err">{uploadError}</p> : null}
          {activeGroup ? (
            <div className="composer-hint">
              <button type="button" onClick={() => setText((v) => (v ? `${v} @everyone` : "@everyone "))}>
                @everyone
              </button>
              {activeGroup.members.map((m) => (
                <button key={m.id} type="button" onClick={() => setText((v) => (v ? `${v} @${m.name}` : `@${m.name} `))}>
                  @{m.name}
                </button>
              ))}
            </div>
          ) : null}
          {uploads.length ? (
            <div className="attach-chips composer-chips">
              {uploads.map((file) => (
                <span key={file.key} className="attach-chip">
                  {file.filename}
                  <button type="button" aria-label="Remove file" onClick={() => setUploads((rows) => rows.filter((f) => f.key !== file.key))}>
                    ×
                  </button>
                </span>
              ))}
            </div>
          ) : null}
          <form className="composer" onSubmit={onSubmit}>
            <input
              ref={fileRef}
              type="file"
              multiple
              hidden
              onChange={async (e) => {
                const picked = Array.from(e.target.files || []);
                e.target.value = "";
                if (!picked.length) return;
                try {
                  setUploadError("");
                  const uploaded = await uploadFiles(picked);
                  setUploads((rows) => [...rows, ...uploaded]);
                } catch (err: any) {
                  setUploadError(err.message || "Upload failed");
                }
              }}
            />
            <button className="attach-icon" type="button" aria-label="Attach files" disabled={busyHere} onClick={() => fileRef.current?.click()}>
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden>
                <path d="M16 7v8.5a4 4 0 0 1-8 0V7.8A2.8 2.8 0 0 1 13.6 7v7.2" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
              </svg>
            </button>
            <textarea
              ref={box}
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder={
                activeGroup
                  ? "Write normally, @ a bot, or @everyone…"
                  : "Ask about sales, mail, or schedule work…"
              }
              rows={1}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  send(text);
                }
              }}
            />
            <button className="send" type="submit" disabled={busyHere || (!text.trim() && !uploads.length)} aria-label="Send">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
                <path d="M12 19V5M12 5 6 11M12 5l6 6" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </button>
          </form>
        </div>
        </div>
      </div>
      {screenOpen ? (
        <>
          <button type="button" className="screen-scrim" aria-label="Close computer" onClick={() => setScreenOpen(false)} />
          <ScreenRail
            session={computer}
            // Only a running turn counts as "operating": an old message that once used the browser must not
            // leave the rail stuck on "Starting computer…" with nothing booting.
            watching={Boolean(busyHere && watching)}
            autoBoot={Boolean(busyHere && watching && computerTurn)}
            controlling={controlling}
            agentName={screenName}
            status={busyHere ? "working" : controlling ? "you have control" : ""}
            onClose={() => setScreenOpen(false)}
            onTakeControl={() => setControlling(true)}
            teaching={teaching}
            onTeachStart={async () => {
              const group = roster.groups.find((row) => row.id === groupId);
              const skillAgentId = agentId || group?.owner_id || group?.members[0]?.id;
              const started = await startTeaching(skillAgentId || undefined);
              setTeaching(true);
              setControlling(true);
              return started;
            }}
            onTeachStop={async (name) => {
              try {
                const saved = await stopTeaching(name);
                setTeaching(false);
                return saved;
              } catch (err: any) {
                // The session was lost (e.g. a deploy restarted the API): leave teaching mode instead of sticking.
                if (String(err?.message || "").includes("No teaching session")) setTeaching(false);
                throw err;
              }
            }}
            onRelease={() => {
              const handoff = pending.filter((p) => p.kind === "handoff" || p.preview?.kind === "handoff");
              setControlling(false);
              if (handoff.length) {
                setPending([]);
                send("I finished on the computer. Continue.", handoff);
              }
            }}
          />
        </>
      ) : null}
      </div>
    </AppShell>
  );
}

function firstName(me: Me | null) {
  const raw = (me?.name || me?.email || "there").trim().split(/[\s@]+/)[0];
  return raw ? raw[0].toUpperCase() + raw.slice(1) : "there";
}

function greeting(me: Me | null) {
  const first = firstName(me);
  const hour = Number(
    new Intl.DateTimeFormat("en-GB", { timeZone: "Asia/Kolkata", hour: "numeric", hour12: false }).format(new Date()),
  );
  const when = hour < 12 ? "Good morning" : hour < 17 ? "Good afternoon" : "Good evening";
  return `${when}, ${first}`;
}

function prettyTool(raw: string) {
  const last = raw.split(/--|__/).pop() || raw;
  return last.replace(/^get_/, "").replace(/_/g, " ");
}

function sourceLabel(meta?: any): string | null {
  if (!meta) return null;
  const names: string[] = [];
  const tools = meta.used_tools;
  if (Array.isArray(tools)) {
    for (const t of tools) {
      const raw = typeof t === "string" ? t : t?.name || t?.tool || t?.slug || "";
      if (raw) names.push(prettyTool(String(raw)));
    }
  }
  if (!names.length && typeof meta.source === "string" && meta.source.trim()) names.push(prettyTool(meta.source));
  if (!names.length && meta.source?.name) names.push(prettyTool(String(meta.source.name)));
  const unique = [...new Set(names.filter(Boolean))];
  return unique.length ? unique.slice(0, 2).join(" · ") : null;
}

