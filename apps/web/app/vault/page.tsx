"use client";

import { useEffect, useMemo, useState } from "react";
import { AppShell } from "../components/AppShell";
import { AgentAvatar } from "../components/AgentAvatar";
import { IconLock, IconPlus, IconVault } from "../components/ProfileIcons";
import { apiError, getMe, loadWorkspace, relativeTime, type Me, type WorkspaceAgent } from "../lib";

// Vault (apps/api/app/browser_runs/logins.py): sign-ins the agents use on sites that have no connector. Encrypted
// with the app key before they are stored; the browser types them, the model only ever sees {{login_password}}.
// Passwords never come back from the API, and they are left out of the data export.
// Each sign-in belongs to the agents it was given to: only they get it, and only they may open its site in the shared
// browser (an agent left out can't reuse a session another agent signed in to).

type SavedLogin = { id: string; host: string; label: string; username_hint: string; agent_ids: string[]; last_used_at: string | null };
type Form = { host: string; label: string; username: string; password: string; changing: boolean; agent_ids: string[] };

const EMPTY: Form = { host: "", label: "", username: "", password: "", changing: false, agent_ids: [] };

/** Tick the agents that may use a sign-in. */
function AgentPicker({ agents, value, onChange }: { agents: WorkspaceAgent[]; value: string[]; onChange: (ids: string[]) => void }) {
  const on = new Set(value);
  return (
    <div className="vault-agents" role="group" aria-label="Agents that may use this sign-in">
      {agents.map((a) => (
        <label key={a.id} className={`vault-agent${on.has(a.id) ? " on" : ""}`}>
          <input
            type="checkbox"
            checked={on.has(a.id)}
            onChange={(e) => onChange(e.target.checked ? [...value, a.id] : value.filter((id) => id !== a.id))}
          />
          <AgentAvatar name={a.name} color={a.color} avatar={a.avatar} className="agent-avatar tiny" />
          <span>{a.name}</span>
        </label>
      ))}
      {agents.length === 0 ? <p className="faint">No agents yet.</p> : null}
    </div>
  );
}

export default function VaultPage() {
  const [me, setMe] = useState<Me | null>(null);
  const [rows, setRows] = useState<SavedLogin[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [form, setForm] = useState<Form | null>(null);
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [agents, setAgents] = useState<WorkspaceAgent[]>([]);
  const [access, setAccess] = useState<{ row: SavedLogin; ids: string[] } | null>(null);

  async function refresh() {
    const res = await fetch("/api/browser/logins", { credentials: "include" });
    if (res.status === 401) {
      window.location.replace("/login");
      return;
    }
    const data = await res.json().catch(() => []);
    if (res.ok && Array.isArray(data)) setRows(data);
    else setError(apiError(data, "Couldn't open your vault."));
    setLoaded(true);
  }

  useEffect(() => {
    getMe().then((u) => {
      if (!u) {
        window.location.replace("/login");
        return;
      }
      setMe(u);
      refresh();
      loadWorkspace()
        .then((w) => setAgents((w.agents || []).filter((a) => !a.archived)))
        .catch(() => undefined);
    });
  }, []);

  useEffect(() => {
    if (!form && !access) return;
    const close = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      setForm(null);
      setAccess(null);
    };
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, [form, access]);

  async function save() {
    if (!form) return;
    setBusy(true);
    setError("");
    try {
      const res = await fetch("/api/browser/logins", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ host: form.host, label: form.label, username: form.username, password: form.password, agent_ids: form.agent_ids }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(apiError(data, "Couldn't save the sign-in."));
      const who = names(form.agent_ids);
      setNotice(
        form.changing
          ? `Updated the sign-in for ${data.host || form.host}.`
          : who
            ? `Saved. ${who} can now sign in to ${data.host || form.host}.`
            : `Saved. No agent can use it yet: choose who may in Access.`,
      );
      setForm(null);
      await refresh();
    } catch (err: any) {
      setError(err.message || "Couldn't save the sign-in.");
    } finally {
      setBusy(false);
    }
  }

  async function saveAccess() {
    if (!access) return;
    setBusy(true);
    setError("");
    try {
      const res = await fetch(`/api/browser/logins/${access.row.id}/agents`, {
        method: "PUT",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ agent_ids: access.ids }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(apiError(data, "Couldn't change who can use it."));
      const who = names(access.ids);
      setNotice(who ? `${who} can use the sign-in for ${access.row.host}. Other agents can't open that site.` : `No agent can use the sign-in for ${access.row.host} now.`);
      setAccess(null);
      await refresh();
    } catch (err: any) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  function names(ids: string[]) {
    const list = ids.map((id) => agents.find((a) => a.id === id)?.name).filter(Boolean) as string[];
    return list.length <= 2 ? list.join(" and ") : `${list.slice(0, -1).join(", ")} and ${list[list.length - 1]}`;
  }

  async function remove(row: SavedLogin) {
    if (!window.confirm(`Remove the sign-in for ${row.host}? Saved runs on that site will ask you to sign in again.`)) return;
    setBusy(true);
    const res = await fetch(`/api/browser/logins/${row.id}`, { method: "DELETE", credentials: "include" });
    setBusy(false);
    if (!res.ok) {
      setError("Couldn't remove the sign-in.");
      return;
    }
    setNotice(`Removed the sign-in for ${row.host}.`);
    await refresh();
  }

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    return q ? rows.filter((r) => `${r.host} ${r.label} ${r.username_hint}`.toLowerCase().includes(q)) : rows;
  }, [rows, query]);

  return (
    <AppShell me={me} current="vault">
      <div className="page">
        <div className="tasks-page">
          <section className="vault-hero">
            <span className="vault-mark" aria-hidden>
              <IconVault size={28} />
            </span>
            <div className="vault-hero-copy">
              <span className="eyebrow">Profile</span>
              <h1>Vault</h1>
              <p>
                Sign-ins your agents use on sites that have no connector, such as a supplier portal or a government site. Add them once
                here, never in chat, and choose which agents may use each one.
              </p>
            </div>
            <button className="btn large" type="button" onClick={() => { setNotice(""); setForm({ ...EMPTY }); }}>
              <IconPlus size={16} /> Add sign-in
            </button>
          </section>

          {error ? <p className="err">{error}</p> : null}
          {notice ? <p className="notice">{notice}</p> : null}

          <div className="vault-list-head">
            <h2>
              Saved sign-ins <span className="pill-count">{rows.length}</span>
            </h2>
            {rows.length > 4 ? (
              <input className="tasks-search" type="search" aria-label="Search the vault" placeholder="Search sites" value={query} onChange={(e) => setQuery(e.target.value)} />
            ) : null}
          </div>

          {loaded && rows.length === 0 ? (
            <div className="task-empty showcase-empty">
              <span className="showcase-icon tone-vault">
                <IconLock size={24} />
              </span>
              <strong>Your vault is empty</strong>
              <p className="muted">Add a sign-in when an agent needs to work on a site for you. Scheduled and saved runs use it automatically.</p>
              <button className="btn" type="button" onClick={() => setForm({ ...EMPTY })}>
                Add your first sign-in
              </button>
            </div>
          ) : null}

          <div className="vault-list">
            {shown.map((row) => (
              <div key={row.id} className="vault-item">
                <span className="vault-monogram" aria-hidden>
                  {(row.label || row.host).slice(0, 1).toUpperCase()}
                </span>
                <div className="vault-item-main">
                  <strong>{row.label || row.host}</strong>
                  <span className="vault-item-meta">
                    <span>{row.host}</span>
                    <span className="vault-user">{row.username_hint}</span>
                    <span className="vault-secret" aria-label="Password saved">
                      ••••••••
                    </span>
                  </span>
                </div>
                <button
                  type="button"
                  className={`vault-access${row.agent_ids.length ? "" : " none"}`}
                  onClick={() => setAccess({ row, ids: row.agent_ids })}
                  aria-label={`Who can use the sign-in for ${row.host}`}
                >
                  {row.agent_ids.length ? (
                    <>
                      <span className="vault-access-faces" aria-hidden>
                        {row.agent_ids.slice(0, 3).map((id) => {
                          const a = agents.find((x) => x.id === id);
                          return a ? <AgentAvatar key={id} name={a.name} color={a.color} avatar={a.avatar} className="agent-avatar tiny" /> : null;
                        })}
                      </span>
                      <span>{names(row.agent_ids) || `${row.agent_ids.length} agents`}</span>
                    </>
                  ) : (
                    <span>No agent can use it</span>
                  )}
                </button>
                <span className="vault-used">{row.last_used_at ? `Used ${relativeTime(row.last_used_at)}` : "Not used yet"}</span>
                <div className="vault-actions">
                  <button
                    className="btn secondary small"
                    type="button"
                    disabled={busy}
                    onClick={() => {
                      setNotice("");
                      setForm({ host: row.host, label: row.label === row.host ? "" : row.label, username: "", password: "", changing: true, agent_ids: row.agent_ids });
                    }}
                  >
                    Change
                  </button>
                  <button className="btn secondary small" type="button" disabled={busy} onClick={() => remove(row)} aria-label={`Remove the sign-in for ${row.host}`}>
                    Remove
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>

      {form ? (
        <div className="sheet run-sheet" onClick={() => setForm(null)}>
          <form
            className="panel vault-form"
            role="dialog"
            aria-modal="true"
            aria-labelledby="vault-form-title"
            autoComplete="off"
            onClick={(e) => e.stopPropagation()}
            onSubmit={(e) => {
              e.preventDefault();
              save();
            }}
          >
            <div className="vault-form-head">
              <span className="vault-promise-icon">
                <IconLock size={16} />
              </span>
              <div>
                <strong id="vault-form-title">{form.changing ? `Change the sign-in for ${form.host}` : "Add a sign-in"}</strong>
                <span className="faint">Encrypted as soon as you save. You won't see the password again.</span>
              </div>
            </div>
            {!form.changing ? (
              <label className="vault-field">
                <span>Site</span>
                <input required autoFocus placeholder="portal.example.com" value={form.host} onChange={(e) => setForm({ ...form, host: e.target.value })} />
              </label>
            ) : null}
            <label className="vault-field">
              <span>
                Name <em>optional</em>
              </span>
              <input placeholder="Supplier portal" value={form.label} onChange={(e) => setForm({ ...form, label: e.target.value })} />
            </label>
            <label className="vault-field">
              <span>Username or email</span>
              <input required autoFocus={form.changing} autoComplete="off" value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} />
            </label>
            <label className="vault-field">
              <span>
                Password {form.changing ? <em>leave empty to keep the saved one</em> : null}
              </span>
              <input type="password" autoComplete="new-password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} />
            </label>
            <div className="vault-field">
              <span>
                Agents that may use it <em>only these can open {form.host.trim() || "this site"}</em>
              </span>
              <AgentPicker agents={agents} value={form.agent_ids} onChange={(ids) => setForm({ ...form, agent_ids: ids })} />
            </div>
            <div className="row vault-form-actions">
              <button type="button" className="btn secondary" onClick={() => setForm(null)}>
                Cancel
              </button>
              <button className="btn" disabled={busy || !form.host.trim() || !form.username.trim()}>
                {busy ? "Saving…" : form.changing ? "Save changes" : "Save to vault"}
              </button>
            </div>
          </form>
        </div>
      ) : null}

      {access ? (
        <div className="sheet run-sheet" onClick={() => setAccess(null)}>
          <div className="panel vault-form" role="dialog" aria-modal="true" aria-labelledby="vault-access-title" onClick={(e) => e.stopPropagation()}>
            <div className="vault-form-head">
              <span className="vault-promise-icon">
                <IconLock size={16} />
              </span>
              <div>
                <strong id="vault-access-title">Who can use {access.row.label || access.row.host}</strong>
                <span className="faint">
                  Only the agents you tick get this sign-in and may open {access.row.host}. Other agents are refused, even if the site is
                  already signed in.
                </span>
              </div>
            </div>
            <AgentPicker agents={agents} value={access.ids} onChange={(ids) => setAccess({ ...access, ids })} />
            <div className="row vault-form-actions">
              <button type="button" className="btn secondary" onClick={() => setAccess(null)}>
                Cancel
              </button>
              <button className="btn" type="button" disabled={busy} onClick={saveAccess}>
                {busy ? "Saving…" : "Save"}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </AppShell>
  );
}
