"use client";

import { FormEvent, useEffect, useRef, useState } from "react";
import { AttachmentWidget } from "../AttachmentWidget";
import { AppShell } from "../components/AppShell";
import { chatAttachments, ComputerIcon, computerFromMeta, ScreenRail } from "../components/ComputerPanel";
import { cardsFromMeta, ConnectCards } from "../components/ConnectCard";
import { EmailThreadButton, mailRefsFromMeta } from "../components/EmailThread";
import { Markdown } from "../components/Markdown";
import { dismissFocusCard, focusCardDismissed, FocusOnboarding, type FocusOption } from "../components/FocusCard";
import { Thinking } from "../components/Thinking";
import { announceWorkspaceChanged, chatHref, updateAgent, deleteSession, getMe, listSessions, loadSession, rememberScope, uploadFiles, type ChatSessionRow, type Me, type UploadedFile, type WorkspaceRoster } from "../lib";

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

/** Poll the API for live status lines of one in-flight chat turn. Returns a stop function. */
function pollProgress(progressId: string, onSteps: (steps: string[]) => void, everyMs = 1200) {
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
        }
        if (data.done) return;
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

function sessionInScopeRow(row: ChatSessionRow | undefined, agentId?: string | null, groupId?: string | null) {
  if (!row) return false;
  if (groupId) return row.group_id === groupId;
  return !row.group_id && !!agentId && row.agent_id === agentId;
}

type Msg = { role: "user" | "assistant"; content: string; meta?: any };
type Confirm = { token: string; title: string; preview: any; kind?: string };
type AgentView = { sessionId: string | null; messages: Msg[]; pending: Confirm[] };

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
  const [notice, setNotice] = useState("");
  const end = useRef<HTMLDivElement>(null);
  const box = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const [uploads, setUploads] = useState<UploadedFile[]>([]);
  const [uploadError, setUploadError] = useState("");
  const [watching, setWatching] = useState(false);
  const [controlling, setControlling] = useState(false);
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
    const view = { sessionId: id, messages: nextMessages, pending: [] as Confirm[] };
    viewsRef.current[scopeKey(nextAgent, nextGroup)] = view;
    setSessionId(id);
    setMessages(nextMessages);
    setPending([]);
    setGroupId(nextGroup);
    setAgentId(nextAgent);
    rememberScope(nextAgent, nextGroup);
    window.localStorage.setItem(scopeStore(nextAgent, nextGroup), id);
    if (typeof window !== "undefined") {
      window.history.replaceState(null, "", chatHref({ agent: nextAgent, group: nextGroup, conversation: id }));
    }
  }

  function newChat() {
    const key = scopeKey(agentId, groupId);
    setSessionId(null);
    setMessages([]);
    setPending([]);
    setNotice("");
    setUploads([]);
    setWatching(false);
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
    const stopProgress = pollProgress(progressId, (steps) =>
      setProgress((all) => (all[key] ? { ...all, [key]: { ...all[key], steps } } : all)),
    );
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
      const raw = await res.text();
      let data: any = {};
      try {
        data = raw ? JSON.parse(raw) : {};
      } catch {
        throw new Error(raw.slice(0, 280) || "Chat failed");
      }
      if (!res.ok) {
        const detail = typeof data.detail === "string" ? data.detail : data.detail?.[0]?.msg;
        throw new Error(detail || raw.slice(0, 280) || "Chat failed");
      }
      if (data.workspace_changed) announceWorkspaceChanged();
      const loaded = data.session_id ? await loadSession(data.session_id) : null;
      const doneMessages: Msg[] = loaded?.messages?.length
        ? loaded.messages
            .filter((m: Msg) => m.role === "user" || m.role === "assistant")
            .map((m: Msg) => ({ role: m.role, content: m.content || "", meta: m.meta }))
        : [
            ...nextMessages,
            { role: "assistant", content: data.text || "", meta: { ...data, connect_cards: cardsFromMeta(data) } },
          ];
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
      const failView: AgentView = {
        sessionId,
        messages: [...nextMessages, { role: "assistant", content: err.message || "Something went wrong" }],
        pending: [],
      };
      viewsRef.current[key] = failView;
      if (scopeRef.current === key) showView(failView);
    } finally {
      stopProgress();
      setProgress((all) => {
        const next = { ...all };
        delete next[key];
        return next;
      });
      inflightRef.current.delete(key);
      setBusyScopes((keys) => keys.filter((k) => k !== key));
      if (scopeRef.current === key) setWatching(false);
    }
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
  const screenMeta = activeGroup ? activeGroup.members.map((m) => m.name).join(" · ") : activeAgent?.title || "";
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
      await updateAgent(id, { focus: option.key });
      setRoster((r) => ({ ...r, agents: r.agents.map((a) => (a.id === id ? { ...a, focus: option.key, focus_label: option.label } : a)) }));
      announceWorkspaceChanged();
    } catch {
      // The agent can still set its own focus from the answer (set_my_focus).
    }
    hideFocusCard(id);
    void send(option.answer);
  }

  const scopedSessions = sessions.filter((s) => sessionInScopeRow(s, agentId, groupId));
  const computer = [...messages].reverse().map((m) => computerFromMeta(m.meta)).find(Boolean) || null;

  function selectAgent(id: string) {
    persistView(scopeKey(agentId, groupId));
    setControlling(false);
    setWatching(false);
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
      apply({ sessionId: pick, messages: nextMessages, pending: [] });
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
                    <p>Here&apos;s what&apos;s happening today — brief the inbox, ask a connected system, or schedule a send.</p>
                  </>
                )}
                <div className="hero-cards">
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
                <div key={i} className="turn assistant">
                  {m.meta?.agent_name ? (
                    <div className="agent-chip">
                      <span className="agent-avatar tiny" style={{ background: m.meta.agent_color || "#f97316" }}>
                        {String(m.meta.agent_name).slice(0, 1)}
                      </span>
                      {m.meta.agent_name}
                    </div>
                  ) : null}
                  <Markdown text={m.content} />
                  <ConnectCards meta={m.meta} />
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
              placeholder="Ask about sales, mail, or schedule work…"
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
            watching={Boolean((busyHere && watching) || controlling || computer?.live)}
            controlling={controlling}
            agentName={screenName}
            status={busyHere ? "working" : controlling ? "you have control" : ""}
            onClose={() => setScreenOpen(false)}
            onTakeControl={() => setControlling(true)}
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

function formatPreview(preview: any): string {
  if (!preview) return "No preview.";
  if (typeof preview === "string") return preview;
  if (preview.repo) {
    const rows: string[] = [`Repo: ${preview.repo}`];
    if (preview.number != null) rows.push(`Pull request: #${preview.number}${preview.method ? ` (${preview.method})` : ""}`);
    if (preview.branch) rows.push(`Branch: ${preview.branch}${preview.base ? ` → ${preview.base}` : ""}`);
    if (preview.head) rows.push(`Head: ${preview.head} → ${preview.base || "default branch"}`);
    if (preview.title) rows.push(`Title: ${preview.title}`);
    if (preview.message) rows.push(`Commit: ${preview.message}`);
    if (preview.files != null) rows.push(`Files: ${preview.files}`);
    if (preview.open_pr) rows.push("Also opens a pull request");
    if (Array.isArray(preview.commits) && preview.commits.length) rows.push("Commits:", ...preview.commits.map((c: string) => `  ${c}`));
    if (preview.diffstat) rows.push(preview.diffstat);
    return rows.join("\n");
  }
  const order = ["folder", "count", "to", "cc", "bcc", "reply_all", "recipient", "subject", "when", "schedule", "body", "preview", "text", "sample", "reason", "instruction"];
  const lines = order
    .filter((k) => preview[k] != null && preview[k] !== "")
    .map((k) => {
      const label = k[0].toUpperCase() + k.slice(1);
      const value = typeof preview[k] === "string" ? preview[k] : JSON.stringify(preview[k]);
      return `${label}: ${value}`;
    });
  return lines.length ? lines.join("\n") : JSON.stringify(preview, null, 2);
}
