"use client";

import { DeployPlatforms } from "../components/DeployPlatforms";
import { FormEvent, useEffect, useMemo, useState } from "react";
import { AppShell } from "../components/AppShell";
import {
  assignConnectorAgents,
  assignToolkitAgents,
  connectMarketplace,
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
  connection_id?: string | null;
  tools_count?: number;
  auth_kind?: string;
  auth_schemes?: string[];
};

type RailItem =
  | { key: string; kind: "connector"; title: string; logo?: string; connected: boolean; connector: Connector }
  | { key: string; kind: "app"; title: string; logo?: string; connected: boolean; app: MarketApp }
  | { key: string; kind: "github"; title: string; connected: boolean }
  | { key: string; kind: "deploy"; title: string; connected: boolean }
  | { key: string; kind: "mcp"; title: string; connected: boolean };

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
  const [marketReady, setMarketReady] = useState<boolean | null>(null);
  const [marketQuery, setMarketQuery] = useState("");
  const [connecting, setConnecting] = useState("");
  const [notice, setNotice] = useState("");
  const [agents, setAgents] = useState<WorkspaceAgent[]>([]);
  const [assignments, setAssignments] = useState<Record<string, string[]>>({});
  const [toolkitAssignments, setToolkitAssignments] = useState<Record<string, string[]>>({});
  const [assigning, setAssigning] = useState("");
  const [github, setGithub] = useState<GithubStatus | null>(null);
  const [selected, setSelected] = useState("");

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
    setToolkitAssignments(roster.toolkit_assignments || {});
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

  async function connectApp(slug: string, credentials?: Record<string, string>) {
    setConnecting(slug);
    setError("");
    try {
      const data = await connectMarketplace(slug, undefined, credentials);
      if (data.redirect_url) {
        window.location.href = data.redirect_url;
        return data;
      }
      if (data.connected) {
        setNotice(`${data.name || slug} connected. Agents can use it from chat.`);
        await loadMarket(marketQuery);
        await refresh();
        return data;
      }
      if (data.needs_credentials) return data;
      throw new Error("Could not connect this app");
    } catch (err: any) {
      setError(err.message);
      return null;
    } finally {
      setConnecting("");
    }
  }

  async function disconnectApp(accountId: string) {
    setConnecting(accountId);
    setError("");
    try {
      const res = await fetch(`/api/connectors/marketplace/connections/${encodeURIComponent(accountId)}`, {
        method: "DELETE",
        credentials: "include",
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Could not disconnect");
      await loadMarket(marketQuery);
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
    const res = await fetch(`/api/connectors/${id}`, { method: "DELETE", credentials: "include" });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      setError(typeof data.detail === "string" ? data.detail : "Could not remove connector");
      return;
    }
    refresh();
  }

  function draftFor(c: Connector) {
    return drafts[c.id] || { alias: c.alias || "", description: c.description || "" };
  }

  const rail = useMemo<RailItem[]>(() => {
    const items: RailItem[] = rows.map((c) => ({
      key: `conn:${c.id}`,
      kind: "connector",
      title: c.alias || c.name,
      connected: c.enabled && c.health === "ok",
      connector: c,
    }));
    items.push({ key: "github", kind: "github", title: "GitHub App", connected: Boolean(github?.installed) });
    items.push({ key: "deploy", kind: "deploy", title: "Deploy", connected: false });
    items.push({ key: "mcp", kind: "mcp", title: "Add MCP", connected: false });
    const taken = new Set(
      rows.flatMap((c) => [c.slug, c.provider].map((value) => String(value || "").toLowerCase()).filter(Boolean)),
    );
    taken.add("composio");
    for (const app of apps) {
      const slug = String(app.slug || "").toLowerCase();
      if (!slug || taken.has(slug)) continue;
      items.push({
        key: `app:${app.slug}`,
        kind: "app",
        title: app.name,
        logo: app.logo,
        connected: Boolean(app.connected),
        app,
      });
    }
    return items;
  }, [rows, apps, github]);

  const filteredRail = useMemo(() => {
    const q = marketQuery.trim().toLowerCase();
    if (!q) return rail;
    return rail.filter((item) => item.title.toLowerCase().includes(q) || item.key.toLowerCase().includes(q));
  }, [rail, marketQuery]);

  useEffect(() => {
    if (filteredRail.some((item) => item.key === selected)) return;
    const live = filteredRail.find((item) => item.connected && item.kind !== "github");
    const next = live || filteredRail.find((item) => item.kind === "app") || filteredRail[0];
    if (next) setSelected(next.key);
  }, [filteredRail, selected]);

  const current = filteredRail.find((item) => item.key === selected) || filteredRail[0];

  return (
    <AppShell me={me} current="connectors">
      <div className="page conn-page">
        <div className="conn-hero page-hero">
          <div>
            <h1>Connectors</h1>
            <p className="muted">Choose an app, then pick which agents can use it.</p>
          </div>
          <form
            className="conn-search"
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
          </form>
        </div>
        {error ? <p className="err">{error}</p> : null}
        {notice ? <p className="ok">{notice}</p> : null}
        {marketReady === false ? (
          <p className="muted">Add a Composio platform API key on the server to list every marketplace app here.</p>
        ) : null}
        <div className="conn-board">
          <section className="conn-catalog">
            <div className="conn-catalog-head">
              <strong>Apps</strong>
              <span className="muted">{filteredRail.length}</span>
            </div>
            <div className="conn-icons" role="list">
              {filteredRail.map((item) => (
                <button
                  key={item.key}
                  type="button"
                  role="listitem"
                  aria-pressed={item.key === selected}
                  className={`conn-icon${item.key === selected ? " on" : ""}${item.connected ? " live" : ""}`}
                  title={item.title}
                  onClick={() => setSelected(item.key)}
                >
                  <RailMark item={item} />
                  <span className="conn-icon-name">{item.title}</span>
                </button>
              ))}
            </div>
          </section>
          <section className="conn-inspector">
            {current?.kind === "github" ? (
              <GithubDetail github={github} onForget={forgetGithub} />
            ) : current?.kind === "deploy" ? (
              <DeployPlatforms />
            ) : current?.kind === "mcp" ? (
              <form className="card stack" onSubmit={add}>
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
                <select value={authType} onChange={(e) => setAuthType(e.target.value)}>
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
            ) : current?.kind === "app" ? (
              <AppDetail
                key={current.app.slug}
                app={current.app}
                agents={agents}
                assignments={toolkitAssignments}
                connecting={connecting}
                assigning={assigning}
                onConnect={(credentials) => connectApp(current.app.slug, credentials)}
                onDisconnect={() => current.app.connection_id && disconnectApp(current.app.connection_id)}
                onAssign={(slug, nextIds, previous) => {
                  setToolkitAssignments((cur) => ({ ...cur, [slug]: nextIds }));
                  setAssigning(slug);
                  setError("");
                  void assignToolkitAgents(slug, nextIds)
                    .then((next) => setToolkitAssignments((cur) => ({ ...cur, [slug]: next.agent_ids || nextIds })))
                    .catch((err: any) => {
                      setToolkitAssignments((cur) => ({ ...cur, [slug]: previous }));
                      setError(err.message || "Could not update agents");
                    })
                    .finally(() => setAssigning(""));
                }}
              />
            ) : current?.kind === "connector" ? (
              <ConnectorDetail
                c={current.connector}
                draft={draftFor(current.connector)}
                open={editing === current.connector.id}
                saving={saving === current.connector.id}
                agents={agents}
                assignments={assignments}
                assigning={assigning === current.connector.id}
                onDraft={(next) => setDrafts((d) => ({ ...d, [current.connector.id]: next }))}
                onEdit={() => setEditing(editing === current.connector.id ? "" : current.connector.id)}
                onSave={() => saveDescribe(current.connector)}
                onPatch={(body) => patch(current.connector.id, body)}
                onRemove={() => remove(current.connector.id)}
                onAssign={(nextIds, previous) => {
                  const id = current.connector.id;
                  setAssignments((cur) => ({ ...cur, [id]: nextIds }));
                  setAssigning(id);
                  setError("");
                  void assignConnectorAgents(id, nextIds)
                    .then((next) => setAssignments((cur) => ({ ...cur, [id]: next.agent_ids || nextIds })))
                    .catch((err: any) => {
                      setAssignments((cur) => ({ ...cur, [id]: previous }));
                      setError(err.message || "Could not update agents");
                    })
                    .finally(() => setAssigning(""));
                }}
              />
            ) : (
              <p className="muted">Select an app to see details.</p>
            )}
          </section>
        </div>
      </div>
    </AppShell>
  );
}

function RailMark({ item }: { item: RailItem }) {
  if (item.kind === "app" && item.logo) return <img src={item.logo} alt="" />;
  if (item.kind === "connector" && item.connector.slug === "gmail") return <GoogleMark />;
  if (item.kind === "connector" && (item.connector.slug === "outlook" || item.connector.provider === "microsoft")) return <MicrosoftMark />;
  const letter = (item.title || "?").slice(0, 1).toUpperCase();
  return <span className="conn-icon-mark">{letter}</span>;
}

function AgentAssign({
  agents,
  restricted,
  assigning,
  onToggle,
}: {
  agents: WorkspaceAgent[];
  restricted: string[];
  assigning: boolean;
  onToggle: (nextIds: string[], previous: string[]) => void;
}) {
  if (!agents.length) return null;
  return (
    <div className="conn-agents">
      <span className="conn-agents-label">Available to</span>
      {agents.map((agent) => {
        const checked = !restricted.length || restricted.includes(agent.id);
        return (
          <label key={agent.id} className="agent-check">
            <input
              type="checkbox"
              checked={checked}
              disabled={assigning}
              onChange={() => {
                onToggle(
                  nextAssignedAgents(
                    restricted,
                    agent.id,
                    agents.map((a) => a.id),
                  ),
                  restricted,
                );
              }}
            />
            <span className="agent-avatar tiny" style={{ background: agent.color }}>
              {(agent.name || "?").slice(0, 1).toUpperCase()}
            </span>
            <span className="agent-check-name">{agent.name}</span>
          </label>
        );
      })}
      <small className="muted">
        {restricted.length ? `${restricted.length} of ${agents.length} agents` : "Every agent"}
      </small>
    </div>
  );
}

function ConnectorDetail({
  c,
  draft,
  open,
  saving,
  agents,
  assignments,
  assigning,
  onDraft,
  onEdit,
  onSave,
  onPatch,
  onRemove,
  onAssign,
}: {
  c: Connector;
  draft: { alias: string; description: string };
  open: boolean;
  saving: boolean;
  agents: WorkspaceAgent[];
  assignments: Record<string, string[]>;
  assigning: boolean;
  onDraft: (next: { alias: string; description: string }) => void;
  onEdit: () => void;
  onSave: () => void;
  onPatch: (body: Record<string, unknown>) => Promise<void>;
  onRemove: () => void;
  onAssign: (nextIds: string[], previous: string[]) => void;
}) {
  const restricted = assignments[c.id] || [];
  const canRemove = c.slug !== "computer";
  return (
    <div className="conn-tile conn-detail-card">
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
            onChange={(e) => onDraft({ ...draft, alias: e.target.value })}
          />
          <textarea
            className="field"
            placeholder="What this MCP can do, brands, metrics, when to use it"
            value={draft.description}
            onChange={(e) => onDraft({ ...draft, description: e.target.value })}
          />
          <button className="btn secondary" disabled={saving} onClick={onSave}>
            {saving ? "Saving…" : "Save description"}
          </button>
        </div>
      ) : null}
      <AgentAssign agents={agents} restricted={restricted} assigning={assigning} onToggle={onAssign} />
      <div className="conn-actions">
        <button className="icon-btn" onClick={onEdit}>
          {open ? "Close" : "Edit"}
        </button>
        <button className="icon-btn" onClick={() => onPatch({ enabled: !c.enabled })}>
          {c.enabled ? "Disable" : "Enable"}
        </button>
        {canRemove ? (
          <button className="icon-btn" onClick={onRemove}>
            Remove
          </button>
        ) : null}
      </div>
    </div>
  );
}

function AppDetail({
  app,
  agents,
  assignments,
  connecting,
  assigning,
  onConnect,
  onDisconnect,
  onAssign,
}: {
  app: MarketApp;
  agents: WorkspaceAgent[];
  assignments: Record<string, string[]>;
  connecting: string;
  assigning: string;
  onConnect: (credentials?: Record<string, string>) => Promise<any>;
  onDisconnect: () => void;
  onAssign: (slug: string, nextIds: string[], previous: string[]) => void;
}) {
  const restricted = assignments[app.slug] || [];
  const kind = app.auth_kind || ((app.auth_schemes || []).includes("API_KEY") ? "api_key" : "oauth");
  const needsKey = kind === "api_key" && !app.connected;
  const [apiKey, setApiKey] = useState("");
  return (
    <div className="conn-tile conn-detail-card">
      <header className="conn-detail-head">
        {app.logo ? <img className="conn-detail-logo" src={app.logo} alt="" /> : <span className="conn-icon-mark lg">{(app.name || "?").slice(0, 1)}</span>}
        <div>
          <strong>{app.name}</strong>
          <div className="conn-count">@{app.slug}{app.tools_count != null ? ` · ${app.tools_count} tools` : ""}</div>
        </div>
        <span className={`badge ${app.connected ? "ok" : "down"}`}>{app.connected ? "connected" : "available"}</span>
      </header>
      <p>{app.description || "Composio marketplace app. Connect it, then choose which agents can use it from chat."}</p>
      {needsKey ? (
        <label className="conn-key-field">
          API key
          <input
            type="password"
            autoComplete="off"
            placeholder={`Paste your ${app.name} API key`}
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
          />
          <small className="muted">Composio does not manage OAuth for this app. Paste the key from the provider dashboard.</small>
        </label>
      ) : null}
      <AgentAssign
        agents={agents}
        restricted={restricted}
        assigning={assigning === app.slug}
        onToggle={(nextIds, previous) => onAssign(app.slug, nextIds, previous)}
      />
      <div className="conn-actions">
        <button
          className="btn"
          disabled={connecting === app.slug || (needsKey && !apiKey.trim())}
          onClick={() => void onConnect(needsKey ? { generic_api_key: apiKey.trim() } : undefined)}
        >
          {connecting === app.slug ? "Connecting…" : app.connected ? "Reconnect" : needsKey ? "Save key" : "Connect"}
        </button>
        {app.connected && app.connection_id ? (
          <button className="icon-btn" disabled={connecting === app.connection_id} onClick={onDisconnect}>
            Remove
          </button>
        ) : null}
      </div>
    </div>
  );
}

function GithubDetail({
  github,
  onForget,
}: {
  github: GithubStatus | null;
  onForget: (id: number) => void;
}) {
  return (
    <div className="card stack">
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
          GitHub MCP server still work in the app catalog.
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
            <button className="btn secondary" onClick={() => onForget(inst.installation_id)}>
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
