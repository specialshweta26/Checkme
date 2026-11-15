"use client";

import { useEffect, useState } from "react";
import { AgentAvatar } from "./AgentAvatar";
import { Select } from "./Select";
import { listRoutines, listSkills, type AgentGroup, type WorkspaceAgent } from "../lib";

// An agent's About panel (docs/DESIGN.md "Side panel"): who it is, what it has, and every option the agent menu has.
// The actions themselves stay in AgentsNav (they refresh the sidebar), so the panel asks it to run them.

export type AgentAction = "pin" | "unread" | "edit" | "connectors" | "duplicate" | "clear" | "archive" | "delete" | "move";
export const AGENT_ACTION_EVENT = "lumio:agent-action";

export function requestAgentAction(agentId: string, action: AgentAction, groupId?: string) {
  window.dispatchEvent(new CustomEvent(AGENT_ACTION_EVENT, { detail: { agentId, action, groupId } }));
}

function Item({ icon, label, hint, onClick, danger }: { icon: string; label: string; hint?: string; onClick: () => void; danger?: boolean }) {
  return (
    <button type="button" className={`about-item${danger ? " danger" : ""}`} onClick={onClick}>
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
        <path d={icon} />
      </svg>
      <span>
        <strong>{label}</strong>
        {hint ? <small>{hint}</small> : null}
      </span>
    </button>
  );
}

export function AgentAboutPanel({ agent, groups, onRoutines }: { agent: WorkspaceAgent; groups: AgentGroup[]; onRoutines: () => void }) {
  const [skills, setSkills] = useState<number | null>(null);
  const [routines, setRoutines] = useState<number | null>(null);
  const [signIns, setSignIns] = useState<string[] | null>(null);
  const [moveTo, setMoveTo] = useState("");
  const isDefault = agent.kind === "default";

  useEffect(() => {
    setSkills(null);
    setRoutines(null);
    setSignIns(null);
    listSkills(agent.id).then((d) => setSkills((d.skills || []).filter((s) => s.agent_id === agent.id).length)).catch(() => setSkills(0));
    listRoutines(agent.id).then((d) => setRoutines(d.tasks.filter((t) => t.status !== "completed").length)).catch(() => setRoutines(0));
    fetch("/api/browser/logins", { credentials: "include" })
      .then((r) => (r.ok ? r.json() : []))
      .then((rows) => setSignIns((Array.isArray(rows) ? rows : []).filter((r) => (r.agent_ids || []).includes(agent.id)).map((r) => r.label || r.host)))
      .catch(() => setSignIns([]));
  }, [agent.id]);

  const act = (a: AgentAction, groupId?: string) => requestAgentAction(agent.id, a, groupId);

  return (
    <div className="about">
      <div className="about-hero">
        <AgentAvatar name={agent.name} color={agent.color} avatar={agent.avatar} className="agent-avatar about-face" />
        <div>
          <strong>{agent.name}</strong>
          {agent.title ? <span>{agent.title}</span> : null}
          <div className="about-tags">
            {isDefault ? <span className="app-pill">Default agent</span> : null}
            {agent.pinned ? <span className="app-pill">Pinned</span> : null}
            {agent.archived ? <span className="app-pill">Archived</span> : null}
          </div>
        </div>
      </div>
      {agent.instructions ? <p className="about-instructions">{agent.instructions}</p> : null}

      <div className="about-facts">
        <button type="button" onClick={() => act("edit")}>
          <strong>{skills ?? "–"}</strong>
          <span>Skills</span>
        </button>
        <button type="button" onClick={onRoutines}>
          <strong>{routines ?? "–"}</strong>
          <span>Routines</span>
        </button>
        <a href="/vault">
          <strong>{signIns?.length ?? "–"}</strong>
          <span>Vault sign-ins</span>
        </a>
        <a href={`/quality?agent=${agent.id}`}>
          <strong>{agent.lessons?.length ?? 0}</strong>
          <span>Lessons</span>
        </a>
      </div>
      {agent.lessons?.length ? (
        <ul className="about-lessons" aria-label="Lessons it follows">
          {agent.lessons.slice(-4).map((l) => (
            <li key={l.id}>{l.text}</li>
          ))}
        </ul>
      ) : null}
      {signIns?.length ? <p className="faint about-note">Can sign in to {signIns.join(", ")}.</p> : null}

      <p className="about-label">Agent</p>
      <div className="about-group">
        <Item icon="M5 16.5 16.5 5l2.5 2.5L7.5 19H5v-2.5z" label="Edit profile" hint="Name, title, instructions, icon and skills" onClick={() => act("edit")} />
        <Item icon="M8 12h8M7 8h3M14 8h3M7 16h3M14 16h3" label="Connectors" hint="The apps this agent may use" onClick={() => act("connectors")} />
        <Item icon="M8 8h10v12H8z M6 16V4h10" label="Duplicate" hint="A copy with the same profile and skills" onClick={() => act("duplicate")} />
      </div>

      <p className="about-label">Sidebar</p>
      <div className="about-group">
        <Item icon="M12 3l2.2 4.6 5 .7-3.6 3.5.9 5.1L12 14.8 7.5 17.9l.9-5.1L4.8 9.3l5-.7z" label={agent.pinned ? "Unpin" : "Pin to top"} onClick={() => act("pin")} />
        <Item icon="M5 7h14v10H5z M5 7l7 6 7-6" label={agent.unread ? "Marked unread" : "Mark as unread"} onClick={() => act("unread")} />
        {groups.length ? (
          <div className="about-move">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
              <path d="M4 6.5h7.5l2 2H20v9H4v-11z" />
            </svg>
            <Select value={moveTo} placeholder="Move to a group…" label="Move to group" options={groups.map((g) => ({ value: g.id, label: g.name }))} onChange={(id) => { setMoveTo(id); act("move", id); }} />
          </div>
        ) : null}
      </div>

      <p className="about-label">Conversation</p>
      <div className="about-group">
        <Item icon="M7 7h10v2H7zM9 9v8M12 9v8M15 9v8M8 19h8" label="Clear conversation" hint="Deletes its chats; profile, skills and routines stay" onClick={() => window.confirm(`Clear ${agent.name}'s conversation?`) && act("clear")} />
        {!isDefault ? (
          <Item icon="M4 7h16v2H4zM6 9h12v10H6z" label={agent.archived ? "Unarchive" : "Archive"} hint={agent.archived ? undefined : "Hide it from the sidebar; nothing is deleted"} onClick={() => act("archive")} />
        ) : null}
        {!isDefault ? (
          <Item icon="M7 7h10l-1 12H8L7 7zM5 7h14M10 4h4" label="Delete agent" hint="Removes it with its chats, skills, memory and routines" danger onClick={() => window.confirm(`Delete ${agent.name}? Its chats, skills, memory and routines go too. This can't be undone.`) && act("delete")} />
        ) : null}
      </div>
    </div>
  );
}
