"use client";

import { useCallback, useEffect, useState, type FormEvent } from "react";
import { AppShell } from "../components/AppShell";
import { apiError, getMe, type Me } from "../lib";

// Teams (apps/api/app/teams/routes.py, docs/TEAMS.md): members and roles, invitations, shared skills, activity log.

type TeamRow = { id: string; name: string; role: Role; members: number };
type Role = "owner" | "admin" | "member" | "viewer";
type Member = { user_id: string; name: string; email: string; role: Role };
type Shared = { id: string; name: string; kind: string; description: string; steps: number; shared_by: string; mine: boolean };
type Invite = { id: string; email: string; role: Role; expired: boolean };
type Detail = { id: string; name: string; role: Role; members: Member[]; skills: Shared[]; invites: Invite[] };
type MySkill = { id: string; name: string; status: string; source?: { team?: string } };

const RANK: Record<Role, number> = { viewer: 0, member: 1, admin: 2, owner: 3 };

async function call(path: string, method = "GET", body?: unknown) {
  const res = await fetch(path, {
    method,
    credentials: "include",
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  return { ok: res.ok, data };
}

export default function TeamsPage() {
  const [me, setMe] = useState<Me | null>(null);
  const [teams, setTeams] = useState<TeamRow[]>([]);
  const [open, setOpen] = useState<string>("");
  const [team, setTeam] = useState<Detail | null>(null);
  const [mySkills, setMySkills] = useState<MySkill[]>([]);
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<Role>("member");
  const [share, setShare] = useState("");
  const [msg, setMsg] = useState("");
  const [error, setError] = useState("");

  const loadTeams = useCallback(async () => {
    const r = await call("/api/teams");
    if (r.ok) {
      setTeams(r.data);
      setOpen((cur) => cur || r.data[0]?.id || "");
    }
  }, []);

  const loadTeam = useCallback(async (id: string) => {
    if (!id) {
      setTeam(null);
      return;
    }
    const r = await call(`/api/teams/${id}`);
    setTeam(r.ok ? r.data : null);
  }, []);

  useEffect(() => {
    getMe().then((u) => {
      if (!u) {
        window.location.replace("/login");
        return;
      }
      setMe(u);
      loadTeams();
      call("/api/skills").then((r) => (r.ok ? setMySkills((r.data.skills || r.data || []) as MySkill[]) : undefined));
    });
  }, [loadTeams]);

  useEffect(() => {
    loadTeam(open);
  }, [open, loadTeam]);

  function done(r: { ok: boolean; data: unknown }, ok: string, fail: string) {
    setMsg(r.ok ? ok : "");
    setError(r.ok ? "" : apiError(r.data, fail));
    loadTeam(open);
    loadTeams();
    return r.ok;
  }

  async function create(e: FormEvent) {
    e.preventDefault();
    const r = await call("/api/teams", "POST", { name });
    if (r.ok) {
      setName("");
      setOpen(r.data.id);
    }
    done(r, "Team created.", "Couldn't create the team.");
  }

  async function invite(e: FormEvent) {
    e.preventDefault();
    const r = await call(`/api/teams/${open}/invites`, "POST", { email, role });
    if (r.ok) setEmail("");
    done(r, r.data.sent ? `Invitation emailed to ${email}.` : `Invited. Mail isn't set up, so send them this link: ${r.data.link}`, "Couldn't invite them.");
  }

  const mine = team ? RANK[team.role] : 0;
  const shareable = mySkills.filter((s) => (s.status || "ready") === "ready");

  return (
    <AppShell me={me} current="teams">
      <div className="page">
        <div className="tasks-page">
          <div className="page-hero">
            <div>
              <h1>Teams</h1>
              <p className="muted">
                Share skills with the people you work with. Your mail, calendar, connectors and memory stay yours: a skill you add from a team runs
                with your own.
              </p>
            </div>
          </div>
          {error ? <p className="err">{error}</p> : null}
          {msg ? <p className="notice">{msg}</p> : null}

          <div className="row wrap">
            {teams.map((t) => (
              <button key={t.id} className={`btn ${open === t.id ? "" : "secondary"}`} onClick={() => setOpen(t.id)}>
                {t.name} <span className="faint">· {t.role}</span>
              </button>
            ))}
            <form className="row" onSubmit={create}>
              <input className="field" placeholder="New team name" value={name} onChange={(e) => setName(e.target.value)} />
              <button className="btn secondary" type="submit" disabled={!name.trim()}>
                Create team
              </button>
            </form>
          </div>

          {team ? (
            <div className="task-list">
              <div className="task-card stack">
                <div className="row spread">
                  <strong>Members ({team.members.length})</strong>
                  {team.role !== "owner" ? (
                    <button
                      className="btn secondary small"
                      onClick={async () => {
                        if (!window.confirm(`Leave ${team.name}?`)) return;
                        const r = await call(`/api/teams/${team.id}/members/${me?.user_id}`, "DELETE");
                        if (done(r, `You left ${team.name}.`, "Couldn't leave.")) setOpen("");
                      }}
                    >
                      Leave team
                    </button>
                  ) : null}
                </div>
                <div className="admin-table">
                  {team.members.map((m) => (
                    <div className="admin-row" key={m.user_id}>
                      <div className="stack">
                        <span>{m.name || m.email}</span>
                        <span className="faint">{m.email}</span>
                      </div>
                      {mine >= RANK.admin && m.role !== "owner" && m.user_id !== me?.user_id ? (
                        <select
                          className="field"
                          value={m.role}
                          onChange={async (e) => done(await call(`/api/teams/${team.id}/members/${m.user_id}`, "PATCH", { role: e.target.value }), "Role changed.", "Couldn't change the role.")}
                        >
                          <option value="viewer">viewer</option>
                          <option value="member">member</option>
                          <option value="admin">admin</option>
                        </select>
                      ) : (
                        <span className="muted">{m.role}</span>
                      )}
                      {mine >= RANK.admin && m.role !== "owner" && m.user_id !== me?.user_id ? (
                        <button className="btn danger small" onClick={async () => done(await call(`/api/teams/${team.id}/members/${m.user_id}`, "DELETE"), "Removed.", "Couldn't remove them.")}>
                          Remove
                        </button>
                      ) : (
                        <span />
                      )}
                    </div>
                  ))}
                </div>
                {mine >= RANK.admin ? (
                  <form className="row wrap" onSubmit={invite}>
                    <input className="field" type="email" placeholder="colleague@company.com" value={email} onChange={(e) => setEmail(e.target.value)} />
                    <select className="field" value={role} onChange={(e) => setRole(e.target.value as Role)}>
                      <option value="member">member</option>
                      <option value="viewer">viewer</option>
                      {team.role === "owner" ? <option value="admin">admin</option> : null}
                    </select>
                    <button className="btn" type="submit" disabled={!email.trim()}>
                      Invite
                    </button>
                  </form>
                ) : null}
                {team.invites.length ? (
                  <ul className="admin-list">
                    {team.invites.map((i) => (
                      <li key={i.id}>
                        <span>
                          {i.email} <span className="faint">· {i.role}{i.expired ? " · expired" : " · waiting"}</span>
                        </span>
                        <button className="btn secondary small" onClick={async () => done(await call(`/api/teams/${team.id}/invites/${i.id}`, "DELETE"), "Invitation cancelled.", "Couldn't cancel it.")}>
                          Cancel
                        </button>
                      </li>
                    ))}
                  </ul>
                ) : null}
              </div>

              <div className="task-card stack">
                <strong>Shared skills</strong>
                <span className="muted">Add one to your own skills to use it. Shared copies never include screenshots, narration or anything typed that looked like a password.</span>
                <ul className="admin-list">
                  {team.skills.map((s) => (
                    <li key={s.id}>
                      <span>
                        {s.name} <span className="faint">· {s.steps} steps · shared by {s.mine ? "you" : s.shared_by}</span>
                      </span>
                      <span className="row">
                        <button className="btn secondary small" onClick={async () => done(await call(`/api/teams/${team.id}/skills/${s.id}/add`, "POST", {}), `Added “${s.name}” to your skills.`, "Couldn't add it.")}>
                          Add to my skills
                        </button>
                        {s.mine || mine >= RANK.admin ? (
                          <button className="btn danger small" onClick={async () => done(await call(`/api/teams/${team.id}/skills/${s.id}`, "DELETE"), "Removed from the team.", "Couldn't remove it.")}>
                            Remove
                          </button>
                        ) : null}
                      </span>
                    </li>
                  ))}
                  {!team.skills.length ? <li className="faint">Nothing shared yet.</li> : null}
                </ul>
                {mine >= RANK.member && shareable.length ? (
                  <div className="row wrap">
                    <select className="field" value={share} onChange={(e) => setShare(e.target.value)}>
                      <option value="">Share one of your skills…</option>
                      {shareable.map((s) => (
                        <option key={s.id} value={s.id}>
                          {s.name}
                        </option>
                      ))}
                    </select>
                    <button
                      className="btn"
                      disabled={!share}
                      onClick={async () => {
                        if (done(await call(`/api/teams/${team.id}/skills`, "POST", { skill_id: share }), "Shared with the team.", "Couldn't share it.")) setShare("");
                      }}
                    >
                      Share
                    </button>
                  </div>
                ) : null}
              </div>

              {mine >= RANK.admin ? (
                <div className="task-card">
                  <div className="task-main">
                    <strong>Activity log</strong>
                    <span className="muted">Joins, roles, invitations and shared skills. Members&apos; own chats and sign-ins aren&apos;t in it.</span>
                  </div>
                  <div className="task-actions">
                    <a className="btn secondary" href={`/api/teams/${team.id}/activity?days=90&fmt=csv`}>
                      Export CSV
                    </a>
                    {team.role === "owner" ? (
                      <button
                        className="btn danger"
                        onClick={async () => {
                          if (!window.confirm(`Delete ${team.name}? Members keep the skills they added.`)) return;
                          if (done(await call(`/api/teams/${team.id}`, "DELETE"), "Team deleted.", "Couldn't delete it.")) setOpen("");
                        }}
                      >
                        Delete team
                      </button>
                    ) : null}
                  </div>
                </div>
              ) : null}
            </div>
          ) : teams.length ? null : (
            <p className="muted">You&apos;re not in a team yet. Create one, or open an invitation link someone sent you.</p>
          )}
        </div>
      </div>
    </AppShell>
  );
}
