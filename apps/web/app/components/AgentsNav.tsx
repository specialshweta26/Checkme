"use client";

import { usePathname } from "next/navigation";
import { FormEvent, useEffect, useMemo, useRef, useState, type MouseEvent, type PointerEvent, type ReactNode } from "react";
import { createPortal } from "react-dom";
import {
  assignConnectorAgents,
  assignToolkitAgents,
  chatHref,
  connectMarketplace,
  clearAgentChat,
  createAgent,
  createGroup,
  deleteAgent,
  deleteGroup,
  duplicateAgent,
  listSkills,
  loadWorkspace,
  moveAgentToGroup,
  nextAssignedAgents,
  saveSkill,
  startTeaching,
  stopTeaching,
  updateAgent,
  WORKSPACE_CHANGED_EVENT,
  type AgentSkill,
  type ChatSessionRow,
  type WorkspaceAgent,
  type WorkspaceRoster,
} from "../lib";

type Ctx = { kind: "agent" | "group"; id: string; x: number; y: number };

const HOLD_MS = 500;
const HOLD_MOVE_PX = 12;

function menuPoint(x: number, y: number) {
  const width = Math.min(260, window.innerWidth - 16);
  const height = Math.min(440, window.innerHeight - 24);
  return {
    x: Math.max(8, Math.min(x, window.innerWidth - width - 8)),
    y: Math.max(8, Math.min(y, window.innerHeight - height - 8)),
  };
}

function useHoldMenu(onOpen: (x: number, y: number) => void) {
  const timer = useRef(0);
  const origin = useRef({ x: 0, y: 0 });
  const holding = useRef(false);
  const opened = useRef(false);
  const pointerId = useRef<number | null>(null);
  const target = useRef<HTMLElement | null>(null);

  function clear(keepOpened = false) {
    if (timer.current) window.clearTimeout(timer.current);
    timer.current = 0;
    holding.current = false;
    pointerId.current = null;
    target.current?.classList.remove("pressing");
    target.current = null;
    if (!keepOpened) opened.current = false;
  }

  useEffect(() => () => clear(), []);

  function openAt(x: number, y: number) {
    opened.current = true;
    onOpen(x, y);
    window.setTimeout(() => {
      opened.current = false;
    }, 1000);
  }

  return {
    onPointerDown(e: PointerEvent<HTMLElement>) {
      if (e.button !== 0) return;
      const node = e.currentTarget;
      target.current = node;
      origin.current = { x: e.clientX, y: e.clientY };
      holding.current = true;
      pointerId.current = e.pointerId;
      opened.current = false;
      node.classList.add("pressing");
      timer.current = window.setTimeout(() => {
        timer.current = 0;
        if (!holding.current) return;
        holding.current = false;
        node.classList.remove("pressing");
        try {
          navigator.vibrate?.(12);
        } catch {
          /* ignore */
        }
        openAt(origin.current.x, origin.current.y);
      }, HOLD_MS);
    },
    onPointerMove(e: PointerEvent<HTMLElement>) {
      if (!holding.current || !timer.current) return;
      if (pointerId.current !== null && e.pointerId !== pointerId.current) return;
      if (Math.hypot(e.clientX - origin.current.x, e.clientY - origin.current.y) > HOLD_MOVE_PX) {
        clear();
      }
    },
    onPointerUp(e: PointerEvent<HTMLElement>) {
      if (pointerId.current !== null && e.pointerId !== pointerId.current) return;
      clear(true);
    },
    onPointerCancel(e: PointerEvent<HTMLElement>) {
      if (pointerId.current !== null && e.pointerId !== pointerId.current) return;
      clear();
    },
    onLostPointerCapture(e: PointerEvent<HTMLElement>) {
      if (pointerId.current !== null && e.pointerId !== pointerId.current) return;
      if (timer.current) clear();
    },
    onContextMenu(e: MouseEvent<HTMLElement>) {
      e.preventDefault();
      e.stopPropagation();
      clear();
      openAt(e.clientX, e.clientY);
    },
    onClickCapture(e: MouseEvent<HTMLElement>) {
      if (!opened.current) return;
      e.preventDefault();
      e.stopPropagation();
    },
  };
}

export function AgentsNav({
  activeAgentId,
  activeGroupId,
  threads = [],
  busyScopes = [],
  onSelectAgent,
  onSelectGroup,
  onRoster,
  onConversationCleared,
}: {
  activeAgentId?: string | null;
  activeGroupId?: string | null;
  threads?: ChatSessionRow[];
  busyScopes?: string[];
  onSelectAgent?: (id: string) => void;
  onSelectGroup?: (id: string) => void;
  onRoster?: (roster: WorkspaceRoster) => void;
  onConversationCleared?: (agentId: string) => void;
}) {
  const [roster, setRoster] = useState<WorkspaceRoster>({ agents: [], groups: [] });
  const [menu, setMenu] = useState(false);
  const [form, setForm] = useState<"agent" | "group" | "edit" | "connectors" | null>(null);
  const [error, setError] = useState("");
  const [name, setName] = useState("");
  const [title, setTitle] = useState("");
  const [instructions, setInstructions] = useState("");
  const [picked, setPicked] = useState<string[]>([]);
  const [outcome, setOutcome] = useState("");
  const [ownerId, setOwnerId] = useState("");
  const [skills, setSkills] = useState<AgentSkill[]>([]);
  const [skillName, setSkillName] = useState("");
  const [skillHint, setSkillHint] = useState("");
  const [teaching, setTeaching] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [ctx, setCtx] = useState<Ctx | null>(null);
  const [moveOpen, setMoveOpen] = useState(false);
  const [showArchived, setShowArchived] = useState(false);
  const [connectors, setConnectors] = useState<{ id: string; name: string; alias?: string; slug: string }[]>([]);
  const [marketApps, setMarketApps] = useState<{ slug: string; name: string; logo?: string; connected?: boolean }[]>([]);
  const [catalogReady, setCatalogReady] = useState(false);
  const [saving, setSaving] = useState(false);
  const pathname = usePathname();

  async function refresh() {
    const next = await loadWorkspace();
    setRoster(next);
    onRoster?.(next);
  }

  useEffect(() => {
    refresh().catch(() => undefined);
  }, []);

  useEffect(() => {
    const onChanged = () => {
      refresh().catch(() => undefined);
    };
    window.addEventListener(WORKSPACE_CHANGED_EVENT, onChanged);
    return () => window.removeEventListener(WORKSPACE_CHANGED_EVENT, onChanged);
  }, []);

  useEffect(() => {
    setMenu(false);
    setCtx(null);
    setMoveOpen(false);
  }, [pathname]);

  useEffect(() => {
    if (!menu) return;
    const close = () => setMenu(false);
    window.addEventListener("click", close);
    return () => window.removeEventListener("click", close);
  }, [menu]);

  useEffect(() => {
    if (!ctx) return;
    const close = () => {
      setCtx(null);
      setMoveOpen(false);
    };
    const t = window.setTimeout(() => {
      window.addEventListener("pointerdown", close);
      window.addEventListener("scroll", close, true);
    }, 400);
    return () => {
      window.clearTimeout(t);
      window.removeEventListener("pointerdown", close);
      window.removeEventListener("scroll", close, true);
    };
  }, [ctx]);

  const agents = roster.agents;
  const groups = roster.groups;
  const liveAgents = agents.filter((a) => !a.archived);
  const archivedAgents = agents.filter((a) => a.archived);

  function resetForm() {
    setForm(null);
    setName("");
    setTitle("");
    setInstructions("");
    setPicked([]);
    setOutcome("");
    setOwnerId("");
    setSkills([]);
    setSkillName("");
    setSkillHint("");
    setTeaching(false);
    setEditingId(null);
    setError("");
    setMenu(false);
    setSaving(false);
    setCatalogReady(false);
  }

  function openEdit(agent: WorkspaceAgent) {
    setEditingId(agent.id);
    setName(agent.name);
    setTitle(agent.title || "");
    setInstructions(agent.instructions || "");
    setForm("edit");
    setCtx(null);
    listSkills(agent.id)
      .then((data) => setSkills(data.skills || []))
      .catch(() => setSkills([]));
  }

  function openConnectors(agent: WorkspaceAgent) {
    setEditingId(agent.id);
    setForm("connectors");
    setCtx(null);
    const jobs: Promise<void>[] = [];
    if (!connectors.length) {
      jobs.push(
        fetch("/api/connectors", { credentials: "include" })
          .then((res) => res.json())
          .then((data) => setConnectors(data.connectors || []))
          .catch(() => undefined),
      );
    }
    if (!marketApps.length) {
      jobs.push(
        fetch("/api/connectors/marketplace", { credentials: "include" })
          .then((res) => res.json())
          .then((data) => setMarketApps(data.apps || []))
          .catch(() => undefined),
      );
    }
    if (!jobs.length) {
      setCatalogReady(true);
      return;
    }
    setCatalogReady(false);
    void Promise.all(jobs).finally(() => setCatalogReady(true));
  }

  async function onCreateAgent(e: FormEvent) {
    e.preventDefault();
    e.stopPropagation();
    if (saving) return;
    setSaving(true);
    setError("");
    try {
      const row = await createAgent({ name, title, instructions });
      resetForm();
      await refresh();
      onSelectAgent?.(row.id);
    } catch (err: any) {
      setError(err.message || "Could not create agent");
      setSaving(false);
    }
  }

  async function onEditAgent(e: FormEvent) {
    e.preventDefault();
    e.stopPropagation();
    if (!editingId || saving) return;
    setSaving(true);
    setError("");
    try {
      await updateAgent(editingId, { name, title, instructions });
      resetForm();
      await refresh();
    } catch (err: any) {
      setError(err.message || "Could not save profile");
      setSaving(false);
    }
  }

  async function onCreateGroup(e: FormEvent) {
    e.preventDefault();
    e.stopPropagation();
    if (saving) return;
    setSaving(true);
    setError("");
    try {
      const row = await createGroup({ name, member_ids: picked, outcome, owner_id: ownerId || picked[0] || null });
      resetForm();
      await refresh();
      onSelectGroup?.(row.id);
    } catch (err: any) {
      setError(err.message || "Could not create group");
      setSaving(false);
    }
  }

  async function patchAgent(id: string, body: Parameters<typeof updateAgent>[1]) {
    await updateAgent(id, body);
    await refresh();
  }

  function onContext(kind: "agent" | "group", id: string, x: number, y: number) {
    setMenu(false);
    setMoveOpen(false);
    const at = menuPoint(x, y);
    setCtx({ kind, id, x: at.x, y: at.y });
  }

  const formOpen = form !== null;
  const ctxAgent = ctx?.kind === "agent" ? agents.find((a) => a.id === ctx.id) : null;

  return (
    <div className="agents-nav">
      <div className="side-label-row">
        <div className="side-label">Personal</div>
        <div className="agents-plus-wrap">
          <button
            className="agents-plus"
            type="button"
            aria-label="Create"
            onClick={(e) => {
              e.stopPropagation();
              setMenu((v) => !v);
            }}
          >
            +
          </button>
          {menu ? (
            <div className="agents-menu" onClick={(e) => e.stopPropagation()}>
              <button type="button" onClick={() => { setMenu(false); setForm("agent"); }}>
                New agent
              </button>
              <button type="button" onClick={() => { setMenu(false); setForm("group"); setPicked(liveAgents.slice(0, 2).map((a) => a.id)); setOwnerId(liveAgents[0]?.id || ""); }}>
                New group
              </button>
            </div>
          ) : null}
        </div>
      </div>
      <div className="agent-list">
        {liveAgents.map((agent) => (
          <AgentRow
            key={agent.id}
            agent={agent}
            href={chatHref({ agent: agent.id })}
            active={activeAgentId === agent.id && !activeGroupId}
            preview={busyScopes.includes(`a.${agent.id}`) ? "Working…" : previewFor(threads, agent.id, null) || agent.preview || agent.title || "New chat"}
            stay={pathname === "/chat"}
            onOpen={() => {
              if (agent.unread) void patchAgent(agent.id, { unread: false });
              onSelectAgent?.(agent.id);
            }}
            onOpenMenu={(x, y) => onContext("agent", agent.id, x, y)}
          />
        ))}
        {groups.map((group) => (
          <HoldRow
            key={group.id}
            className={`agent-row${activeGroupId === group.id ? " on" : ""}`}
            label={group.name}
            onOpenMenu={(x, y) => onContext("group", group.id, x, y)}
          >
            <a
              className="agent-open"
              href={chatHref({ group: group.id })}
              draggable={false}
              onClick={(e) => {
                e.preventDefault();
                if (pathname === "/chat") {
                  onSelectGroup?.(group.id);
                  return;
                }
                window.location.assign(chatHref({ group: group.id }));
              }}
            >
              <GroupStack members={group.members} />
              <span className="agent-copy">
                <strong>{group.name}</strong>
                <small>{busyScopes.includes(`g.${group.id}`) ? "Working…" : previewFor(threads, null, group.id) || group.outcome || group.preview || group.members.map((m) => m.name).join(", ") || "Group"}</small>
              </span>
            </a>
          </HoldRow>
        ))}
        {archivedAgents.length ? (
          <button className="agent-archived-toggle" type="button" onClick={() => setShowArchived((v) => !v)}>
            {showArchived ? "Hide archived" : `Archived (${archivedAgents.length})`}
          </button>
        ) : null}
        {showArchived
          ? archivedAgents.map((agent) => (
              <AgentRow
                key={agent.id}
                agent={agent}
                href={chatHref({ agent: agent.id })}
                active={activeAgentId === agent.id && !activeGroupId}
                preview="Archived"
                stay={pathname === "/chat"}
                onOpen={() => onSelectAgent?.(agent.id)}
                onOpenMenu={(x, y) => onContext("agent", agent.id, x, y)}
              />
            ))
          : null}
      </div>
      {ctx && ctxAgent
        ? createPortal(
        <div
          className="agent-ctx"
          style={{ left: ctx.x, top: ctx.y }}
          onPointerDown={(e) => e.stopPropagation()}
          onClick={(e) => e.stopPropagation()}
          onContextMenu={(e) => e.preventDefault()}
        >
          <button type="button" onClick={() => { void patchAgent(ctxAgent.id, { pinned: !ctxAgent.pinned }); setCtx(null); }}>
            <CtxIcon d="M12 3l2.2 4.6 5 .7-3.6 3.5.9 5.1L12 14.8 7.5 17.9l.9-5.1L4.8 9.3l5-.7z" />
            {ctxAgent.pinned ? "Unpin" : "Pin"}
          </button>
          <div className="agent-ctx-sub">
            <button type="button" className="agent-ctx-parent" onClick={() => setMoveOpen((v) => !v)}>
              <CtxIcon d="M4 6.5h7.5l2 2H20v9H4v-11z" />
              Move to
              <span className="agent-ctx-chevron">›</span>
            </button>
            {moveOpen ? (
              <div className="agent-ctx-flyout">
                {groups.length ? (
                  groups.map((group) => (
                    <button
                      key={group.id}
                      type="button"
                      onClick={async () => {
                        await moveAgentToGroup(ctxAgent.id, group.id);
                        setCtx(null);
                        await refresh();
                      }}
                    >
                      {group.name}
                    </button>
                  ))
                ) : (
                  <p className="muted">Create a group first</p>
                )}
              </div>
            ) : null}
          </div>
          <button type="button" onClick={() => { void patchAgent(ctxAgent.id, { unread: true }); setCtx(null); }}>
            <CtxIcon d="M5 7h14v10H5z M5 7l7 6 7-6" />
            {ctxAgent.unread ? "Marked unread" : "Mark as Unread"}
          </button>
          <hr />
          <button type="button" onClick={() => openEdit(ctxAgent)}>
            <CtxIcon d="M5 16.5 16.5 5l2.5 2.5L7.5 19H5v-2.5z" />
            Edit Profile
          </button>
          <button
            type="button"
            onClick={async () => {
              const row = await duplicateAgent(ctxAgent.id);
              setCtx(null);
              await refresh();
              onSelectAgent?.(row.id);
            }}
          >
            <CtxIcon d="M8 8h10v12H8z M6 16V4h10" />
            Duplicate
          </button>
          <button type="button" onClick={() => openConnectors(ctxAgent)}>
            <CtxIcon d="M8 12h8M7 8h3M14 8h3M7 16h3M14 16h3" />
            Connectors
          </button>
          <hr />
          <button
            type="button"
            onClick={async () => {
              await clearAgentChat(ctxAgent.id);
              setCtx(null);
              onConversationCleared?.(ctxAgent.id);
              await refresh();
            }}
          >
            <CtxIcon d="M7 7h10v2H7zM9 9v8M12 9v8M15 9v8M8 19h8" />
            Clear conversation
          </button>
          {ctxAgent.kind !== "default" ? (
            <button type="button" onClick={() => { void patchAgent(ctxAgent.id, { archived: !ctxAgent.archived }); setCtx(null); }}>
              <CtxIcon d="M4 7h16v2H4zM6 9h12v10H6z" />
              {ctxAgent.archived ? "Unarchive" : "Archive"}
            </button>
          ) : null}
          {ctxAgent.kind !== "default" ? (
            <button
              type="button"
              className="danger"
              onClick={async () => {
                const id = ctxAgent.id;
                await deleteAgent(id);
                setCtx(null);
                const next = await loadWorkspace();
                setRoster(next);
                onRoster?.(next);
                if (activeAgentId === id) onSelectAgent?.(next.default_agent_id || next.agents[0]?.id || "");
              }}
            >
              <CtxIcon d="M7 7h10l-1 12H8L7 7zM5 7h14M10 4h4" />
              Delete
            </button>
          ) : null}
        </div>,
            document.body,
          )
        : null}
      {ctx?.kind === "group"
        ? createPortal(
        <div
          className="agent-ctx"
          style={{ left: ctx.x, top: ctx.y }}
          onPointerDown={(e) => e.stopPropagation()}
          onClick={(e) => e.stopPropagation()}
          onContextMenu={(e) => e.preventDefault()}
        >
          <button
            type="button"
            className="danger"
            onClick={async () => {
              await deleteGroup(ctx.id);
              setCtx(null);
              await refresh();
            }}
          >
            Delete
          </button>
        </div>,
            document.body,
          )
        : null}
      {formOpen
        ? createPortal(
        <div className="agent-form-scrim" onClick={resetForm}>
          <form
            className={`agent-form${form === "connectors" ? " wide" : ""}`}
            onClick={(e) => e.stopPropagation()}
            onSubmit={form === "connectors" ? (e) => e.preventDefault() : form === "group" ? onCreateGroup : form === "edit" ? onEditAgent : onCreateAgent}
          >
            <h3>{form === "group" ? "New group" : form === "edit" ? "Edit profile" : form === "connectors" ? "Connectors" : "New agent"}</h3>
            {error ? <p className="err">{error}</p> : null}
            {form === "connectors" && editingId ? (
              <ConnectorPicker
                agentId={editingId}
                agents={agents}
                connectors={connectors}
                apps={marketApps}
                catalogReady={catalogReady}
                assignments={roster.connector_assignments || {}}
                toolkitAssignments={roster.toolkit_assignments || {}}
                onSave={async (connectorId, agentIds) => {
                  await assignConnectorAgents(connectorId, agentIds);
                }}
                onSaveToolkit={async (toolkit, agentIds) => {
                  await assignToolkitAgents(toolkit, agentIds);
                }}
                onRefresh={refresh}
                onError={setError}
                onConnectApp={async (slug) => {
                  const data = await connectMarketplace(slug, editingId);
                  if (data.needs_credentials) {
                    throw new Error("This app uses an API key. Open Connectors, paste the key, then assign it here.");
                  }
                  if (data.redirect_url) window.open(data.redirect_url, "_blank", "noreferrer");
                  await refresh();
                }}
              />
            ) : (
              <>
                <label>
                  Name
                  <input value={name} onChange={(e) => setName(e.target.value)} required maxLength={80} placeholder={form === "group" ? "Ops team" : "Researcher"} />
                </label>
                {form === "group" ? (
                  <>
                    <label>
                      Outcome
                      <textarea
                        value={outcome}
                        onChange={(e) => setOutcome(e.target.value)}
                        rows={3}
                        maxLength={400}
                        placeholder="What should this group finish, and who owns the next step?"
                      />
                    </label>
                    <fieldset>
                      <legend>Bots (2–6)</legend>
                      {liveAgents.length ? liveAgents.map((agent) => {
                        const on = picked.includes(agent.id);
                        return (
                          <label key={agent.id} className="agent-check">
                            <input
                              type="checkbox"
                              checked={on}
                              disabled={!on && picked.length >= 6}
                              onChange={() => {
                                const next = picked.includes(agent.id)
                                  ? picked.filter((id) => id !== agent.id)
                                  : [...picked, agent.id];
                                setPicked(next);
                                if (ownerId && !next.includes(ownerId)) setOwnerId(next[0] || "");
                              }}
                            />
                            {agent.name}
                          </label>
                        );
                      }) : (
                        <p className="muted">Create at least two agents first.</p>
                      )}
                    </fieldset>
                    <label>
                      Kickoff owner
                      <select value={ownerId || picked[0] || ""} onChange={(e) => setOwnerId(e.target.value)}>
                        {picked.map((id) => {
                          const agent = agents.find((a) => a.id === id);
                          return (
                            <option key={id} value={id}>
                              {agent?.name || id}
                            </option>
                          );
                        })}
                      </select>
                    </label>
                    <p className="muted">One owner per stage. Write normally, @ a bot to hand a request, or @everyone for updates.</p>
                  </>
                ) : (
                  <>
                    <label>
                      Title
                      <input value={title} onChange={(e) => setTitle(e.target.value)} maxLength={80} placeholder="analyst" />
                    </label>
                    <label>
                      Instructions
                      <textarea value={instructions} onChange={(e) => setInstructions(e.target.value)} rows={5} placeholder="How this agent should work…" />
                    </label>
                    {form === "edit" && editingId ? (
                      <SkillEditor
                        agentId={editingId}
                        skills={skills}
                        skillName={skillName}
                        skillHint={skillHint}
                        teaching={teaching}
                        onSkillName={setSkillName}
                        onSkillHint={setSkillHint}
                        onSkills={setSkills}
                        onTeaching={setTeaching}
                        onError={setError}
                      />
                    ) : null}
                  </>
                )}
              </>
            )}
            <div className="agent-form-actions">
              <button className="btn secondary" type="button" onClick={resetForm}>
                {form === "connectors" ? "Done" : "Cancel"}
              </button>
              {form !== "connectors" ? (
                <button
                  className="btn"
                  type="submit"
                  disabled={saving || (form === "group" && (picked.length < 2 || picked.length > 6))}
                >
                  {saving ? "Saving…" : form === "edit" ? "Save" : "Create"}
                </button>
              ) : null}
            </div>
          </form>
        </div>,
            document.body,
          )
        : null}
    </div>
  );
}

function HoldRow({
  className,
  label,
  onOpenMenu,
  children,
}: {
  className: string;
  label: string;
  onOpenMenu: (x: number, y: number) => void;
  children: ReactNode;
}) {
  const hold = useHoldMenu(onOpenMenu);
  return (
    <div className={className} {...hold}>
      {children}
      <button
        type="button"
        className="agent-more"
        aria-label={`More actions for ${label}`}
        onPointerDown={(e) => e.stopPropagation()}
        onClick={(e) => {
          e.preventDefault();
          e.stopPropagation();
          const box = e.currentTarget.getBoundingClientRect();
          onOpenMenu(box.right - 8, box.bottom + 4);
        }}
      >
        ···
      </button>
    </div>
  );
}

function AgentRow({
  agent,
  href,
  active,
  preview,
  stay,
  onOpen,
  onOpenMenu,
}: {
  agent: WorkspaceAgent;
  href: string;
  active: boolean;
  preview: string;
  stay?: boolean;
  onOpen: () => void;
  onOpenMenu: (x: number, y: number) => void;
}) {
  return (
    <HoldRow
      className={`agent-row${active ? " on" : ""}${agent.unread ? " unread" : ""}`}
      label={agent.name}
      onOpenMenu={onOpenMenu}
    >
      <a
        className="agent-open"
        href={href}
        draggable={false}
        onClick={(e) => {
          e.preventDefault();
          if (stay) {
            onOpen();
            return;
          }
          window.location.assign(href);
        }}
      >
        <span className="agent-avatar" style={{ background: agent.color }}>
          {(agent.name || "?").slice(0, 1).toUpperCase()}
        </span>
        <span className="agent-copy">
          <strong>
            {agent.pinned ? <span className="agent-pin" aria-hidden /> : null}
            {agent.name}
          </strong>
          <small>{preview}</small>
        </span>
        {agent.unread ? <span className="agent-unread" aria-label="Unread" /> : null}
      </a>
    </HoldRow>
  );
}

function SkillEditor({
  agentId,
  skills,
  skillName,
  skillHint,
  teaching,
  onSkillName,
  onSkillHint,
  onSkills,
  onTeaching,
  onError,
}: {
  agentId: string;
  skills: AgentSkill[];
  skillName: string;
  skillHint: string;
  teaching: boolean;
  onSkillName: (v: string) => void;
  onSkillHint: (v: string) => void;
  onSkills: (rows: AgentSkill[]) => void;
  onTeaching: (v: boolean) => void;
  onError: (v: string) => void;
}) {
  async function refresh() {
    const data = await listSkills(agentId);
    onSkills(data.skills || []);
  }
  return (
    <fieldset>
      <legend>Skills</legend>
      <p className="muted">Save a workflow, or teach one by demonstration while the bot watches the screen. Teaching records up to ten minutes of visible screen activity and no microphone audio.</p>
      {skills.length ? (
        <ul className="skill-list">
          {skills.map((skill) => (
            <li key={skill.id}>
              <strong>{skill.name}</strong>
              <span className="muted"> {skill.kind === "taught" ? "taught" : "saved"}</span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted">No skills yet.</p>
      )}
      <label>
        Skill name
        <input value={skillName} onChange={(e) => onSkillName(e.target.value)} maxLength={160} placeholder="Weekly KPI pull" />
      </label>
      <label>
        How to run it
        <textarea value={skillHint} onChange={(e) => onSkillHint(e.target.value)} rows={3} placeholder="Steps this bot should repeat…" />
      </label>
      <div className="agent-form-actions">
        <button
          className="btn secondary"
          type="button"
          onClick={async () => {
            onError("");
            try {
              await saveSkill({ name: skillName, agent_id: agentId, instruction: skillHint });
              onSkillName("");
              onSkillHint("");
              await refresh();
            } catch (err: any) {
              onError(err.message || "Could not save skill");
            }
          }}
        >
          Save workflow
        </button>
        {teaching ? (
          <button
            className="btn"
            type="button"
            onClick={async () => {
              onError("");
              try {
                await stopTeaching(skillName);
                onTeaching(false);
                onSkillName("");
                await refresh();
              } catch (err: any) {
                onError(err.message || "Could not stop teaching");
              }
            }}
          >
            Stop teaching
          </button>
        ) : (
          <button
            className="btn secondary"
            type="button"
            onClick={async () => {
              onError("");
              try {
                await startTeaching(agentId);
                onTeaching(true);
              } catch (err: any) {
                onError(err.message || "Could not start teaching");
              }
            }}
          >
            Teach from screen
          </button>
        )}
      </div>
      {teaching ? <p className="muted">Watching the computer screen (max 10 minutes). Microphone is off.</p> : null}
    </fieldset>
  );
}

function ConnectorPicker({
  agentId,
  agents,
  connectors,
  apps,
  catalogReady,
  assignments,
  toolkitAssignments,
  onSave,
  onSaveToolkit,
  onRefresh,
  onConnectApp,
  onError,
}: {
  agentId: string;
  agents: WorkspaceAgent[];
  connectors: { id: string; name: string; alias?: string; slug: string }[];
  apps: { slug: string; name: string; logo?: string; connected?: boolean }[];
  catalogReady: boolean;
  assignments: Record<string, string[]>;
  toolkitAssignments: Record<string, string[]>;
  onSave: (connectorId: string, agentIds: string[]) => Promise<void>;
  onSaveToolkit: (toolkit: string, agentIds: string[]) => Promise<void>;
  onRefresh: () => Promise<void>;
  onConnectApp: (slug: string) => Promise<void>;
  onError?: (message: string) => void;
}) {
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState("");
  const q = query.trim().toLowerCase();
  const shownConnectors = connectors.filter((conn) => {
    if (!q) return true;
    return `${conn.alias || ""} ${conn.name} ${conn.slug}`.toLowerCase().includes(q);
  });
  const shownApps = apps.filter((app) => {
    if (!app.connected) return false;
    if (!q) return true;
    return `${app.name} ${app.slug}`.toLowerCase().includes(q);
  });
  if (!connectors.length && !apps.some((app) => app.connected)) {
    return (
      <p className="muted">
        {catalogReady
          ? "No connected apps yet. Connect them on the Connectors page, then assign them here."
          : "Loading connectors…"}
      </p>
    );
  }
  const agentIds = agents.map((a) => a.id);
  const shown = [
    ...shownConnectors.map((conn) => {
      const restricted = assignments[conn.id] || [];
      return { key: conn.id, kind: "connector" as const, conn, restricted, checked: !restricted.length || restricted.includes(agentId) };
    }),
    ...shownApps.map((app) => {
      const restricted = toolkitAssignments[app.slug] || [];
      return { key: app.slug, kind: "app" as const, app, restricted, checked: !restricted.length || restricted.includes(agentId) };
    }),
  ];
  const checkedCount = shown.filter((row) => row.checked).length;
  const allChecked = shown.length > 0 && checkedCount === shown.length;
  const someChecked = checkedCount > 0 && !allChecked;

  async function toggleShown(want: boolean) {
    setBusy("all");
    try {
      const jobs = shown.filter((row) => row.checked !== want);
      for (let i = 0; i < jobs.length; i += 8) {
        await Promise.all(
          jobs.slice(i, i + 8).map((row) =>
            row.kind === "connector"
              ? onSave(row.conn.id, nextAssignedAgents(row.restricted, agentId, agentIds))
              : onSaveToolkit(row.app.slug, nextAssignedAgents(row.restricted, agentId, agentIds)),
          ),
        );
      }
      await onRefresh();
    } catch (err: any) {
      onError?.(err.message || "Could not update connectors");
    } finally {
      setBusy("");
    }
  }

  return (
    <fieldset className="conn-picker">
      <legend>This agent can use</legend>
      <input
        type="search"
        placeholder="Search connectors"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
      />
      <div className="conn-picker-list">
        {shown.length ? (
          <label className="agent-check conn-picker-all">
            <input
              type="checkbox"
              checked={allChecked}
              disabled={Boolean(busy)}
              ref={(el) => {
                if (el) el.indeterminate = someChecked;
              }}
              onChange={() => void toggleShown(!allChecked)}
            />
            <span className="agent-check-name">
              {allChecked ? "Deselect all" : q ? "Select all matches" : "Select all"}
              <span className="muted">
                {" "}
                {checkedCount} of {shown.length}
              </span>
            </span>
          </label>
        ) : null}
        {shownConnectors.map((conn) => {
          const restricted = assignments[conn.id] || [];
          const checked = !restricted.length || restricted.includes(agentId);
          return (
            <label key={conn.id} className="agent-check">
              <input
                type="checkbox"
                checked={checked}
                disabled={Boolean(busy)}
                onChange={async () => {
                  setBusy(conn.id);
                  try {
                    await onSave(conn.id, nextAssignedAgents(restricted, agentId, agentIds));
                    await onRefresh();
                  } finally {
                    setBusy("");
                  }
                }}
              />
              <span className="conn-picker-mark">{(conn.alias || conn.name || "?").slice(0, 1).toUpperCase()}</span>
              <span className="agent-check-name">
                {conn.alias || conn.name}
                <span className="muted"> @{conn.slug}</span>
              </span>
            </label>
          );
        })}
        {shownApps.map((app) => {
          const restricted = toolkitAssignments[app.slug] || [];
          const checked = !restricted.length || restricted.includes(agentId);
          return (
            <label key={`app-${app.slug}`} className="agent-check">
              <input
                type="checkbox"
                checked={checked}
                disabled={Boolean(busy)}
                onChange={async () => {
                  setBusy(app.slug);
                  try {
                    if (!checked && !app.connected) await onConnectApp(app.slug);
                    await onSaveToolkit(app.slug, nextAssignedAgents(restricted, agentId, agentIds));
                    await onRefresh();
                  } catch (err: any) {
                    onError?.(err.message || "Could not connect this app");
                  } finally {
                    setBusy("");
                  }
                }}
              />
              {app.logo ? (
                <img className="conn-picker-logo" src={app.logo} alt="" loading="lazy" decoding="async" width={20} height={20} />
              ) : (
                <span className="conn-picker-mark" />
              )}
              <span className="agent-check-name">
                {app.name}
                <span className="muted"> {app.connected ? "connected" : "available"}</span>
              </span>
            </label>
          );
        })}
      </div>
    </fieldset>
  );
}

function CtxIcon({ d }: { d: string }) {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden>
      <path d={d} stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  );
}

function previewFor(threads: ChatSessionRow[], agentId: string | null, groupId: string | null) {
  const row = threads.find((s) => (groupId ? s.group_id === groupId : s.agent_id === agentId && !s.group_id));
  return (row?.title || "").trim();
}

function GroupStack({ members }: { members: WorkspaceAgent[] }) {
  const shown = useMemo(() => members.slice(0, 3), [members]);
  return (
    <span className="group-stack" aria-hidden>
      {shown.map((m) => (
        <span key={m.id} className="agent-avatar tiny" style={{ background: m.color }}>
          {(m.name || "?").slice(0, 1).toUpperCase()}
        </span>
      ))}
    </span>
  );
}
