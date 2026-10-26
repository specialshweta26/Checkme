"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
import {
  chatHref,
  deleteSession,
  initials,
  listSessions,
  markInboxSeen,
  rememberScope,
  rememberedScope,
  relativeTime,
  subscribeInbox,
  type ChatSessionRow,
  type InboxAlert,
  type Me,
  type WorkspaceRoster,
} from "../lib";
import { AgentsNav } from "./AgentsNav";
import { BrandMark } from "./BrandMark";

type Nav = "chat" | "inbox" | "tasks" | "connectors";

function IconChat() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden>
      <path d="M5 6.5h14v9H9l-4 3v-12Z" stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round" />
    </svg>
  );
}

function IconInbox() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden>
      <path d="M4 7.5h16v10H4v-10Z" stroke="currentColor" strokeWidth="1.8" />
      <path d="M4 13h4.2l1.3 2h5l1.3-2H20" stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round" />
    </svg>
  );
}

function IconBell() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden>
      <path d="M6 9.5a6 6 0 0 1 12 0c0 5 1.5 6.5 1.5 6.5H4.5S6 14.5 6 9.5Z" stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round" />
      <path d="M10 18.5a2 2 0 0 0 4 0" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
    </svg>
  );
}

function IconClock() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden>
      <circle cx="12" cy="12" r="8" stroke="currentColor" strokeWidth="1.8" />
      <path d="M12 8v4.5l3 2" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
    </svg>
  );
}

function IconPlug() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden>
      <path d="M9 7v4M15 7v4M8 11h8v3a4 4 0 0 1-8 0v-3Z" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M12 18v2" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
    </svg>
  );
}

function IconComputer() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" aria-hidden>
      <rect x="3.5" y="4.5" width="17" height="12" rx="2" stroke="currentColor" strokeWidth="1.8" />
      <path d="M8 20h8M12 16.5V20" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
    </svg>
  );
}

function NotifyBell({
  count,
  items,
  toast,
  open,
  onToggle,
}: {
  count: number;
  items: InboxAlert[];
  toast: string;
  open: boolean;
  onToggle: () => void;
}) {
  return (
    <div className="notify-wrap">
      <button
        className={`notify-bell${count > 0 ? " live" : ""}${open ? " open" : ""}`}
        type="button"
        aria-label={count ? `${count} emails need action` : "Inbox notifications"}
        aria-expanded={open}
        onClick={onToggle}
      >
        <IconBell />
        {count > 0 ? <span className="notify-dot">{count > 9 ? "9+" : count}</span> : null}
      </button>
      {toast && !open ? <span className="notify-toast">{toast}</span> : null}
      {open ? (
        <div className="notify-panel">
          <div className="notify-panel-head">
            <strong>{count > 0 ? "New mail" : "Notifications"}</strong>
            <a href="/inbox">Open inbox</a>
          </div>
          {items.length === 0 ? (
            <p className="muted notify-empty">No new emails.</p>
          ) : (
            <div className="notify-list">
              {items.map((mail) => (
                <a key={mail.id} className="notify-item" href={`/inbox?mail=${mail.id}`}>
                  <span className={`triage-tag ${mail.classification || "pending"}`}>
                    {mail.classification || "reviewing"}
                  </span>
                  <strong>
                    {mail.recipient_role === "cc" || mail.recipient_role === "bcc" ? (
                      <span className="role-tag">{mail.recipient_role === "bcc" ? "Bcc" : "Cc"} </span>
                    ) : null}
                    {mail.subject || "(no subject)"}
                  </strong>
                  <small>{mail.from || "Unknown sender"}{mail.date ? ` · ${mail.date}` : ""}</small>
                </a>
              ))}
            </div>
          )}
        </div>
      ) : null}
    </div>
  );
}

export function AppShell({
  me,
  current,
  conversations,
  threads,
  activeId,
  activeAgentId,
  activeGroupId,
  onNewChat,
  onOpenChat,
  onDeleteChat,
  onSelectAgent,
  onSelectGroup,
  onRoster,
  onConversationCleared,
  busyScopes,
  headline,
  headlineMeta,
  headlineColor,
  onToggleComputer,
  computerOpen,
  computerLive,
  children,
}: {
  me?: Me | null;
  current: Nav;
  conversations?: ChatSessionRow[];
  threads?: ChatSessionRow[];
  activeId?: string | null;
  activeAgentId?: string | null;
  activeGroupId?: string | null;
  busyScopes?: string[];
  onNewChat?: () => void;
  onOpenChat?: (id: string) => void;
  onDeleteChat?: (id: string) => void;
  onSelectAgent?: (id: string) => void;
  onSelectGroup?: (id: string) => void;
  onRoster?: (roster: WorkspaceRoster) => void;
  onConversationCleared?: (agentId: string) => void;
  headline?: string;
  headlineMeta?: string;
  headlineColor?: string;
  onToggleComputer?: () => void;
  computerOpen?: boolean;
  computerLive?: boolean;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const [scope, setScope] = useState({ agent: "", group: "" });
  const [own, setOwn] = useState<ChatSessionRow[]>([]);
  const [badge, setBadge] = useState(0);
  const [pending, setPending] = useState(0);
  const [alerts, setAlerts] = useState<InboxAlert[]>([]);
  const [alertsOpen, setAlertsOpen] = useState(false);
  const [toast, setToast] = useState("");
  const last = useRef(-1);
  const seenGen = useRef(0);
  const alertsOpenRef = useRef(false);

  useEffect(() => {
    setScope(rememberedScope());
  }, []);

  useEffect(() => {
    if (conversations) return;
    listSessions().then(setOwn);
  }, [conversations]);

  useEffect(() => {
    let on = true;
    const stop = subscribeInbox((next) => {
      if (!on) return;
      const gen = seenGen.current;
      if (gen !== seenGen.current) return;
      if (next.count > last.current && last.current >= 0 && next.count > 0) {
        setToast(next.count === 1 ? "New email arrived" : `${next.count} new emails arrived`);
        window.setTimeout(() => {
          if (on) setToast("");
        }, 4200);
      }
      last.current = next.count;
      setBadge(next.count);
      setPending(next.pending);
      if (next.items.length || !alertsOpenRef.current) setAlerts(next.items);
      window.dispatchEvent(new CustomEvent("inbox-live", { detail: next }));
    });
    return () => {
      on = false;
      stop();
    };
  }, []);

  useEffect(() => {
    function onSeen() {
      seenGen.current += 1;
      last.current = 0;
      setBadge(0);
      setToast("");
    }
    window.addEventListener("inbox-seen", onSeen);
    return () => window.removeEventListener("inbox-seen", onSeen);
  }, []);

  useEffect(() => {
    if (!alertsOpen) return;
    function close(ev: MouseEvent) {
      const target = ev.target as HTMLElement | null;
      if (target?.closest(".notify-wrap")) return;
      alertsOpenRef.current = false;
      setAlertsOpen(false);
    }
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [alertsOpen]);

  function toggleAlerts() {
    setAlertsOpen((open) => {
      const nextOpen = !open;
      alertsOpenRef.current = nextOpen;
      if (nextOpen) {
        seenGen.current += 1;
        last.current = 0;
        setBadge(0);
        setToast("");
        const gen = seenGen.current;
        markInboxSeen().then((next) => {
          if (gen !== seenGen.current) return;
          setBadge(next.count);
          setPending(next.pending);
          if (next.items.length) setAlerts(next.items);
        });
      }
      return nextOpen;
    });
  }

  const rows = conversations || own;
  const titles: Record<Nav, string> = { chat: "Chat", inbox: "Inbox", tasks: "Tasks", connectors: "Connectors" };
  const agentId = activeAgentId || scope.agent || null;
  const groupId = activeGroupId || scope.group || null;

  function closeMenu() {
    setOpen(false);
  }

  function chatUrl(extra: { agent?: string | null; group?: string | null; conversation?: string | null; fresh?: boolean } = {}) {
    return chatHref({
      agent: extra.agent ?? agentId,
      group: extra.group ?? groupId,
      conversation: extra.conversation,
      fresh: extra.fresh,
    });
  }

  function visit(href: string) {
    closeMenu();
    if (typeof window !== "undefined") window.location.assign(href);
  }

  function goAgent(id: string) {
    rememberScope(id, null);
    setScope({ agent: id, group: "" });
    if (onSelectAgent && current === "chat") {
      onSelectAgent(id);
      closeMenu();
      return;
    }
    visit(chatHref({ agent: id }));
  }

  function goGroup(id: string) {
    rememberScope(null, id);
    setScope({ agent: "", group: id });
    if (onSelectGroup && current === "chat") {
      onSelectGroup(id);
      closeMenu();
      return;
    }
    visit(chatHref({ group: id }));
  }

  function goNew(e: { preventDefault: () => void }) {
    e.preventDefault();
    if (onNewChat && current === "chat") {
      onNewChat();
      closeMenu();
      return;
    }
    visit(chatUrl({ fresh: true }));
  }

  function goOpen(e: { preventDefault: () => void }, id: string) {
    e.preventDefault();
    if (onOpenChat && current === "chat") {
      onOpenChat(id);
      closeMenu();
      return;
    }
    const row = rows.find((s) => s.id === id);
    visit(chatHref({ agent: row?.agent_id || agentId, group: row?.group_id || groupId, conversation: id }));
  }

  async function goDelete(id: string) {
    if (onDeleteChat) {
      onDeleteChat(id);
      return;
    }
    await deleteSession(id);
    setOwn((rows) => rows.filter((r) => r.id !== id));
  }

  return (
    <div className="shell">
      <div className={`scrim${open ? " show" : ""}`} onClick={() => setOpen(false)} />
      <aside className={`sidebar${open ? " open" : ""}`}>
        <div className="side-brand-row">
          <a className="side-brand" href={chatUrl()} onClick={(e) => { e.preventDefault(); visit(chatUrl()); }}>
            <BrandMark size={28} />
            <div>
              <strong>Assistant</strong>
              <small>Workspace</small>
            </div>
          </a>
          <NotifyBell count={badge} items={alerts} toast={toast} open={alertsOpen} onToggle={toggleAlerts} />
        </div>
        <a className="side-new" href={chatUrl({ fresh: true })} onClick={goNew}>
          New chat
        </a>
        <nav className="side-nav">
          <a className={`side-link${current === "chat" ? " on" : ""}`} href={chatUrl()} onClick={(e) => { e.preventDefault(); visit(chatUrl()); }}>
            <IconChat /> Chat
          </a>
          <a className={`side-link${current === "inbox" ? " on" : ""}`} href="/inbox" onClick={(e) => { e.preventDefault(); visit("/inbox"); }}>
            <IconInbox /> Inbox
            {pending > 0 ? <span className="nav-count">{pending > 9 ? "9+" : pending}</span> : null}
          </a>
          <a className={`side-link${current === "tasks" ? " on" : ""}`} href="/tasks" onClick={(e) => { e.preventDefault(); visit("/tasks"); }}>
            <IconClock /> Tasks
          </a>
          <a className={`side-link${current === "connectors" ? " on" : ""}`} href="/connectors" onClick={(e) => { e.preventDefault(); visit("/connectors"); }}>
            <IconPlug /> Connectors
          </a>
        </nav>
        <AgentsNav
          activeAgentId={agentId}
          activeGroupId={groupId}
          threads={threads || conversations}
          busyScopes={busyScopes}
          onSelectAgent={goAgent}
          onSelectGroup={goGroup}
          onRoster={onRoster}
          onConversationCleared={onConversationCleared}
        />
        <div className="side-label">Recent</div>
        <div className="convo-list">
          {rows.length === 0 ? <div className="muted" style={{ padding: "8px 10px", fontSize: 13 }}>No conversations yet</div> : null}
          {rows.map((s) => (
            <div key={s.id} className={`convo${activeId === s.id ? " on" : ""}`}>
              <a
                className="convo-open"
                href={chatHref({ agent: s.agent_id || agentId, group: s.group_id || groupId, conversation: s.id })}
                onClick={(e) => goOpen(e, s.id)}
              >
                <div className="convo-title">{s.title || "Untitled"}</div>
                <div className="convo-meta">{relativeTime(s.updated_at)}</div>
              </a>
              <button className="convo-x" type="button" title="Delete conversation" onClick={() => goDelete(s.id)}>
                ×
              </button>
            </div>
          ))}
        </div>
        <div className="side-user">
          <div className="who">
            <span className="avatar">{me ? initials(me.name, me.email) : "…"}</span>
            <div>
              <strong>{me?.name || "Workspace"}</strong>
              <span>{me?.email || "Loading…"}</span>
            </div>
          </div>
          <form action="/api/auth/logout" method="post">
            <button className="logout" type="submit">
              Sign out
            </button>
          </form>
        </div>
      </aside>
      <div className="main">
        <header className="topbar">
          <button className="ghost topbar-who" type="button" onClick={() => setOpen(true)} aria-label="Open menu">
            {headlineColor ? (
              <span className="agent-avatar tiny" style={{ background: headlineColor }}>
                {(headline || "?").slice(0, 1).toUpperCase()}
              </span>
            ) : (
              <BrandMark size={22} />
            )}
            <span className="topbar-copy">
              <strong>{headline || titles[current]}</strong>
              {headlineMeta ? <small>{headlineMeta}</small> : null}
            </span>
          </button>
          <div className="topbar-actions">
            {onToggleComputer ? (
              <button
                type="button"
                className={`topbar-icon${computerOpen ? " on" : ""}${computerLive ? " live" : ""}`}
                aria-label={computerOpen ? "Hide computer" : "Open computer"}
                aria-pressed={Boolean(computerOpen)}
                onClick={onToggleComputer}
              >
                <IconComputer />
              </button>
            ) : null}
            <NotifyBell count={badge} items={alerts} toast={toast} open={alertsOpen} onToggle={toggleAlerts} />
          </div>
        </header>
        {children}
      </div>
    </div>
  );
}
