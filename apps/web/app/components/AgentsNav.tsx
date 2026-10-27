"use client";

import { usePathname } from "next/navigation";
import { FormEvent, useEffect, useMemo, useState, type MouseEvent } from "react";
import { createPortal } from "react-dom";
import {
  assignConnectorAgents,
  chatHref,
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
  const [saving, setSaving] = useState(false);
  const pathname = usePathname();

  async function refresh() {
    const next = await loadWorkspace();
    setRoster(next);
    onRoster?.(next);
  }

  useEffect(() => {
    refresh().catch(() => undefined);
    fetch("/api/connectors", { credentials: "include" })
      .then((res) => res.json())
      .then((data) => setConnectors(data.connectors || []))
      .catch(() => undefined);
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
    window.addEventListener("click", close);
    window.addEventListener("scroll", close, true);
    return () => {
      window.removeEventListener("click", close);
      window.removeEventListener("scroll", close, true);
    };
  }, [ctx]);

  const agents = roster.agents;
  const groups = roster.groups;
  const liveAgents = agents.filter((a) => !a.archived);
  const archivedAgents = agents.filter((a) => a.archived);
  const assignments = roster.connector_assignments || {};

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

  function onContext(e: MouseEvent, kind: "agent" | "group", id: string) {
    e.preventDefault();
    e.stopPropagation();
    setMenu(false);
    setMoveOpen(false);
    const x = Math.min(e.clientX, window.innerWidth - 240);
    const y = Math.min(e.clientY, window.innerHeight - 320);
    setCtx({ kind, id, x, y });
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
            onContext={(e) => onContext(e, "agent", agent.id)}
          />
        ))}
        {groups.map((group) => (
          <div
            key={group.id}
            className={`agent-row${activeGroupId === group.id ? " on" : ""}`}
            onContextMenu={(e) => onContext(e, "group", group.id)}
          >
            <a
              className="agent-open"
              href={chatHref({ group: group.id })}
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
          </div>
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
                onContext={(e) => onContext(e, "agent", agent.id)}
              />
            ))
          : null}
      </div>
      {ctx && ctxAgent
        ? createPortal(
        <div className="agent-ctx" style={{ left: ctx.x, top: ctx.y }} onClick={(e) => e.stopPropagation()} onContextMenu={(e) => e.preventDefault()}>
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
        <div className="agent-ctx" style={{ left: ctx.x, top: ctx.y }} onClick={(e) => e.stopPropagation()} onContextMenu={(e) => e.preventDefault()}>
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
            className="agent-form"
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
                assignments={assignments}
                onSave={async (connectorId, agentIds) => {
                  await assignConnectorAgents(connectorId, agentIds);
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

function AgentRow({
  agent,
  href,
  active,
  preview,
  stay,
  onOpen,
  onContext,
}: {
  agent: WorkspaceAgent;
  href: string;
  active: boolean;
  preview: string;
  stay?: boolean;
  onOpen: () => void;
  onContext: (e: MouseEvent) => void;
}) {
  return (
    <div className={`agent-row${active ? " on" : ""}${agent.unread ? " unread" : ""}`} onContextMenu={onContext}>
      <a
        className="agent-open"
        href={href}
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
    </div>
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
  assignments,
  onSave,
}: {
  agentId: string;
  agents: WorkspaceAgent[];
  connectors: { id: string; name: string; alias?: string; slug: string }[];
  assignments: Record<string, string[]>;
  onSave: (connectorId: string, agentIds: string[]) => Promise<void>;
}) {
  if (!connectors.length) {
    return <p className="muted">No connectors yet. Add them on the Connectors page, then assign them here.</p>;
  }
  return (
    <fieldset>
      <legend>This agent can use</legend>
      {connectors.map((conn) => {
        const restricted = assignments[conn.id] || [];
        const checked = !restricted.length || restricted.includes(agentId);
        return (
          <label key={conn.id} className="agent-check">
            <input
              type="checkbox"
              checked={checked}
              onChange={async () => {
                await onSave(conn.id, nextAssignedAgents(restricted, agentId, agents.map((a) => a.id)));
              }}
            />
            {conn.alias || conn.name}
            <span className="muted"> @{conn.slug}</span>
          </label>
        );
      })}
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
