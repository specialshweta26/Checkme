"use client";

import { DeployPlatforms } from "../components/DeployPlatforms";
import { FormEvent, useEffect, useState } from "react";
import { AppShell } from "../components/AppShell";
import {
  assignConnectorAgents,
  errorFromQuery,
  getMe,
  loadWorkspace,
  nextAssignedAgents,
  type Me,
  type WorkspaceAgent,
} from "../lib";

type Connector = {
  id: string;
  name: string;
  alias?: string;
  description?: string;
  slug: string;
  kind: string;
  provider: string;
  enabled: boolean;
  health: string;
  health_detail?: string;
  tags: string[];
  default_for: string[];
  tool_count?: number;
  card?: string;
};

type GithubStatus = {
  configured: boolean;
  can_verify: boolean;
  missing_env: string[];
  installed: boolean;
  install_url: string;
  app_url: string;
  installations: {
    installation_id: number;
    account: string;
    account_type: string;
    repository_selection: string;
    suspended: boolean;
    manage_url: string;
  }[];
};

type MarketApp = {
  slug: string;
  name: string;
  description?: string;
  logo?: string;
  connected?: boolean;
  tools_count?: number;
};

export default function ConnectorsPage() {
  const [me, setMe] = useState<Me | null>(null);
  const [rows, setRows] = useState<Connector[]>([]);
  const [error, setError] = useState("");
  const [url, setUrl] = useState("");
  const [token, setToken] = useState("");
  const [authType, setAuthType] = useState("oauth");
  const [alias, setAlias] = useState("");
  const [description, setDescription] = useState("");
  const [busy, setBusy] = useState(false);
  const [drafts, setDrafts] = useState<Record<string, { alias: string; description: string }>>({});
  const [saving, setSaving] = useState<string>("");
  const [editing, setEditing] = useState<string>("");
  const [apps, setApps] = useState<MarketApp[]>([]);
  const [marketReady, setMarketReady] = useState(false);
  const [marketQuery, setMarketQuery] = useState("");
  const [connecting, setConnecting] = useState("");
  const [notice, setNotice] = useState("");
  const [agents, setAgents] = useState<WorkspaceAgent[]>([]);
  const [assignments, setAssignments] = useState<Record<string, string[]>>({});
  const [assigning, setAssigning] = useState("");
  const [github, setGithub] = useState<GithubStatus | null>(null);

  async function loadGithub() {
    const res = await fetch("/api/connectors/github/status", { credentials: "include" });
    if (res.ok) setGithub(await res.json());
  }

  async function forgetGithub(id: number) {
    await fetch(`/api/connectors/github/installations/${id}`, { method: "DELETE", credentials: "include" });
    await loadGithub();
    await refresh();
  }

  async function refresh() {
    const res = await fetch("/api/connectors", { credentials: "include" });
    if (res.status === 401) {
      window.location.replace("/");
      return;
    }
    const data = await res.json();
    setRows(data.connectors || []);
    const roster = await loadWorkspace();
    setAgents((roster.agents || []).filter((a) => !a.archived));
    setAssignments(roster.connector_assignments || {});
  }

  useEffect(() => {
    getMe().then((user) => {
      if (!user) {
        window.location.replace("/");
        return;
      }
      setMe(user);
    });
    setError(errorFromQuery(window.location.search));
    const q = new URLSearchParams(window.location.search);
    if (q.get("composio") === "connected") setNotice("Composio app connected. You can use it from chat.");
    if (q.get("github") === "connected") setNotice("GitHub App installed. Your Coder can read repos and open PRs with your approval.");
    if (q.get("github") === "requested") setNotice("Install request sent to the GitHub org owner. Come back once they approve it.");
    refresh();
    loadGithub();
    loadMarket("");
  }, []);

  async function loadMarket(q: string) {
    const res = await fetch(`/api/connectors/marketplace?query=${encodeURIComponent(q)}`, { credentials: "include" });
    if (!res.ok) return;
    const data = await res.json();
    setMarketReady(Boolean(data.configured));
    setApps(data.apps || []);
  }

  async function connectApp(slug: string) {
    setConnecting(slug);
    setError("");
    try {
      const res = await fetch("/api/connectors/marketplace/connect", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ toolkit: slug }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Could not start Composio sign-in");
      if (data.redirect_url) {
        window.location.href = data.redirect_url;
        return;
      }
      throw new Error("No sign-in URL returned");
    } catch (err: any) {
      setError(err.message);
    } finally {
      setConnecting("");
    }
  }

  async function add(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const res = await fetch("/api/connectors", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          url,
          auth_type: authType,
          token: token || undefined,
          alias: alias || undefined,
          description: description || undefined,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Could not add MCP");
      if (data.start_url) {
        window.location.href = data.start_url;
        return;
      }
      if (data.oauth_url) {
        window.location.href = data.oauth_url;
        return;
      }
      setUrl("");
      setToken("");
      setAlias("");
      setDescription("");
      await refresh();
    } catch (err: any) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function patch(id: string, body: Record<string, unknown>) {
    const res = await fetch(`/api/connectors/${id}`, {
      method: "PATCH",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(typeof data.detail === "string" ? data.detail : "Could not update connector");
    }
    await refresh();
  }

  async function saveDescribe(c: Connector) {
    const draft = drafts[c.id] || { alias: c.alias || "", description: c.description || "" };
    setSaving(c.id);
    setError("");
    try {
      await patch(c.id, { alias: draft.alias, description: draft.description });
      setEditing("");
    } catch (err: any) {
      setError(err.message);
    } finally {
      setSaving("");
    }
  }

  async function remove(id: string) {
    await fetch(`/api/connectors/${id}`, { method: "DELETE", credentials: "include" });
    refresh();
  }

  function draftFor(c: Connector) {
    return drafts[c.id] || { alias: c.alias || "", description: c.description || "" };
  }

  return (
    <AppShell me={me} current="connectors">
      <div className="page">
        <div className="conn-hero page-hero">
          <div>
            <h1>Connectors</h1>
            <p className="muted">Link mail, a cloud computer, and marketplace apps, then choose which agents can use each one.</p>
          </div>
        </div>
        <div className="conn-connect">
          <a className="conn-brand" href="/api/auth/google/login?mode=link">
            <GoogleMark />
            <span>
              <strong>Gmail</strong>
              <span>Mail, calendar, and attachments</span>
            </span>
          </a>
          <a className="conn-brand" href="/api/auth/microsoft/login?mode=link">
            <MicrosoftMark />
            <span>
              <strong>Outlook</strong>
              <span>Outlook mail and calendar</span>
            </span>
          </a>
        </div>
        {error ? <p className="err">{error}</p> : null}
        {notice ? <p className="ok">{notice}</p> : null}
        <div className="card stack" style={{ marginBottom: 18 }}>
          <header className="row" style={{ justifyContent: "space-between" }}>
            <strong>GitHub</strong>
            <span className={`badge ${github?.installed ? "ok" : "down"}`}>
              {!github ? "checking" : !github.configured ? "not set up" : github.installed ? "installed" : "not installed"}
            </span>
          </header>
          <p className="muted" style={{ margin: 0 }}>
            Install the Checkme GitHub App on the repos your Coder should work on. It reads code, pushes only to agent/*
            branches, and every push, PR, and merge waits for your approval.
          </p>
          {github && !github.configured ? (
            <p className="muted" style={{ margin: 0 }}>
              The GitHub App is not configured on this server yet ({github.missing_env.join(", ")}). Composio GitHub or a
              GitHub MCP server still work below.
            </p>
          ) : null}
          {github?.installations.map((inst) => (
            <div key={inst.installation_id} className="row" style={{ justifyContent: "space-between" }}>
              <span>
                @{inst.account} · {inst.repository_selection === "all" ? "all repos" : "selected repos"}
                {inst.suspended ? " · suspended" : ""}
              </span>
              <span className="row">
                <a className="btn secondary" href={inst.manage_url} target="_blank" rel="noreferrer">
                  Manage on GitHub
                </a>
                <button className="btn secondary" onClick={() => forgetGithub(inst.installation_id)}>
                  Forget
                </button>
              </span>
            </div>
          ))}
          {github?.configured ? (
            <div>
              <a className="btn" href={github.install_url}>
                {github.installed ? "Add another account or repo" : "Install GitHub App"}
              </a>
            </div>
          ) : null}
        </div>
        <DeployPlatforms />
        {marketReady ? (
          <div className="card stack" style={{ marginBottom: 18 }}>
            <strong>Composio marketplace</strong>
            <p className="muted" style={{ margin: 0 }}>
              Connect GitHub, Slack, Notion, and other apps. The assistant searches and runs their tools from chat.
            </p>
            <form
              className="row"
              onSubmit={(e) => {
                e.preventDefault();
                loadMarket(marketQuery);
              }}
            >
              <input
                type="search"
                placeholder="Search apps"
                value={marketQuery}
                onChange={(e) => setMarketQuery(e.target.value)}
              />
              <button className="btn secondary" type="submit">
                Search
              </button>
            </form>
            <div className="conn-grid">
              {apps.map((app) => (
                <div key={app.slug} className="conn-tile">
                  <header>
                    <strong>{app.name}</strong>
                    <span className={`badge ${app.connected ? "ok" : "down"}`}>{app.connected ? "connected" : "available"}</span>
                  </header>
                  <p>{app.description || "Marketplace app"}</p>
                  <div className="conn-count">
                    @{app.slug}
                    {app.tools_count != null ? ` · ${app.tools_count} tools` : ""}
                  </div>
                  <button className="btn" disabled={connecting === app.slug} onClick={() => connectApp(app.slug)}>
                    {connecting === app.slug ? "Opening…" : app.connected ? "Reconnect" : "Connect"}
                  </button>
                </div>
              ))}
            </div>
          </div>
        ) : null}
        <form className="card stack" onSubmit={add} style={{ marginBottom: 18 }}>
          <strong>Add a system</strong>
          <p className="muted" style={{ margin: 0 }}>
            Point at an MCP server. The assistant will discover its tools after you connect.
          </p>
          <input type="url" required placeholder="https://mcp.example.com/mcp" value={url} onChange={(e) => setUrl(e.target.value)} />
          <input type="text" placeholder="Short name, e.g. digital" value={alias} onChange={(e) => setAlias(e.target.value)} />
          <textarea
            className="field"
            placeholder="Describe what this connector is for. Example: daily sales, retail KPIs, finance reports."
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
          <select
            value={authType}
            onChange={(e) => setAuthType(e.target.value)}
          >
            <option value="oauth">OAuth (MCP spec)</option>
            <option value="bearer">Bearer token</option>
            <option value="none">No auth</option>
          </select>
          {authType === "bearer" ? (
            <input type="text" placeholder="Token" value={token} onChange={(e) => setToken(e.target.value)} />
          ) : null}
          <button className="btn" disabled={busy}>
            {busy ? "Connecting…" : "Connect"}
          </button>
        </form>
        <div className="conn-grid">
          {rows.map((c) => {
            const draft = draftFor(c);
            const open = editing === c.id;
            return (
              <div key={c.id} className="conn-tile">
                <header>
                  <strong>{c.alias || c.name}</strong>
                  <span className={`badge ${c.health === "ok" ? "ok" : "down"}`}>{c.health}</span>
                </header>
                <p>{c.description || "No description yet. Add one so chat knows when to use this."}</p>
                <div className="conn-count">
                  {c.kind} · @{c.slug}
                  {c.tool_count != null ? ` · ${c.tool_count} tools` : ""}
                </div>
                {c.kind === "mcp" && (c.health === "pending" || c.health === "auth_expired") ? (
                  <a className="btn" href={`/api/connectors/${c.id}/oauth/start`}>
                    Finish sign-in
                  </a>
                ) : null}
                {c.health === "auth_expired" && c.provider !== "mcp" ? (
                  <a className="btn" href={c.provider === "gmail" ? "/api/auth/google/login?mode=link" : "/api/auth/microsoft/login?mode=link"}>
                    Reconnect
                  </a>
                ) : null}
                {open ? (
                  <div className="stack">
                    <input
                      type="text"
                      placeholder="Short name the assistant should recognize"
                      value={draft.alias}
                      onChange={(e) => setDrafts((d) => ({ ...d, [c.id]: { ...draft, alias: e.target.value } }))}
                    />
                    <textarea
                      className="field"
                      placeholder="What this MCP can do, brands, metrics, when to use it"
                      value={draft.description}
                      onChange={(e) => setDrafts((d) => ({ ...d, [c.id]: { ...draft, description: e.target.value } }))}
                    />
                    <button className="btn secondary" disabled={saving === c.id} onClick={() => saveDescribe(c)}>
                      {saving === c.id ? "Saving…" : "Save description"}
                    </button>
                  </div>
                ) : null}
                {agents.length ? (
                  <div className="conn-agents">
                    <span className="conn-agents-label">Available to</span>
                    {agents.map((agent) => {
                      const restricted = assignments[c.id] || [];
                      const checked = !restricted.length || restricted.includes(agent.id);
                      return (
                        <label key={agent.id} className="agent-check">
                          <input
                            type="checkbox"
                            checked={checked}
                            onChange={() => {
                              const nextIds = nextAssignedAgents(
                                restricted,
                                agent.id,
                                agents.map((a) => a.id),
                              );
                              setAssignments((cur) => ({ ...cur, [c.id]: nextIds }));
                              setAssigning(c.id);
                              setError("");
                              void assignConnectorAgents(c.id, nextIds)
                                .then((next) => setAssignments((cur) => ({ ...cur, [c.id]: next.agent_ids || nextIds })))
                                .catch((err: any) => {
                                  setAssignments((cur) => ({ ...cur, [c.id]: restricted }));
                                  setError(err.message || "Could not update agents");
                                })
                                .finally(() => setAssigning(""));
                            }}
                          />
                          <span className="agent-avatar tiny" style={{ background: agent.color }}>
                            {(agent.name || "?").slice(0, 1).toUpperCase()}
                          </span>
                          {agent.name}
                        </label>
                      );
                    })}
                    <small className="muted">
                      {(assignments[c.id] || []).length
                        ? `${assignments[c.id].length} of ${agents.length} agents`
                        : "Every agent"}
                    </small>
                  </div>
                ) : null}
                <div className="row">
                  <button className="icon-btn" onClick={() => setEditing(open ? "" : c.id)}>
                    {open ? "Close" : "Edit"}
                  </button>
                  <button className="icon-btn" onClick={() => patch(c.id, { enabled: !c.enabled })}>
                    {c.enabled ? "Disable" : "Enable"}
                  </button>
                  {c.kind !== "builtin" ? (
                    <button className="icon-btn" onClick={() => remove(c.id)}>
                      Remove
                    </button>
                  ) : null}
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </AppShell>
  );
}

function GoogleMark() {
  return (
    <svg width="22" height="22" viewBox="0 0 24 24" aria-hidden>
      <path fill="#4285F4" d="M23.5 12.3c0-.8-.1-1.6-.2-2.3H12v4.4h6.5c-.3 1.5-1.1 2.8-2.4 3.7v3h3.9c2.3-2.1 3.5-5.2 3.5-8.8z" />
      <path fill="#34A853" d="M12 24c3.2 0 5.9-1 7.9-2.9l-3.9-3c-1.1.7-2.4 1.2-4 1.2-3.1 0-5.7-2.1-6.6-4.9H1.4v3.1C3.4 21.4 7.4 24 12 24z" />
      <path fill="#FBBC05" d="M5.4 14.4c-.2-.7-.4-1.4-.4-2.4s.1-1.7.4-2.4V6.5H1.4C.5 8.3 0 10.1 0 12s.5 3.7 1.4 5.5l4-3.1z" />
      <path fill="#EA4335" d="M12 4.8c1.7 0 3.3.6 4.5 1.8l3.4-3.4C17.9 1.1 15.2 0 12 0 7.4 0 3.4 2.6 1.4 6.5l4 3.1C6.3 6.8 8.9 4.8 12 4.8z" />
    </svg>
  );
}

function MicrosoftMark() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" aria-hidden>
      <path fill="#F25022" d="M1 1h10v10H1z" />
      <path fill="#7FBA00" d="M13 1h10v10H13z" />
      <path fill="#00A4EF" d="M1 13h10v10H1z" />
      <path fill="#FFB900" d="M13 13h10v10H13z" />
    </svg>
  );
}
