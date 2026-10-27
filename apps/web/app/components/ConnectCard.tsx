"use client";

import { useState } from "react";
import { connectMarketplace, grantToolkitAgent } from "../lib";

export type ConnectCardView = {
  toolkit?: string;
  name?: string;
  logo?: string;
  description?: string;
  redirect_url?: string;
  needs_credentials?: boolean;
  fields?: { name: string; label?: string; secret?: boolean }[];
  agent_id?: string;
  agent_name?: string;
};

function asCards(value: unknown): ConnectCardView[] {
  if (Array.isArray(value)) return value.filter((row) => row && typeof row === "object") as ConnectCardView[];
  if (typeof value === "string") {
    try {
      return asCards(JSON.parse(value));
    } catch {
      return [];
    }
  }
  return [];
}

export function cardsFromMeta(meta: any): ConnectCardView[] {
  const out: ConnectCardView[] = [];
  const seen = new Set<string>();
  const add = (row: any) => {
    if (!row || typeof row !== "object") return;
    const url = String(row.redirect_url || row.redirectUrl || "").trim();
    const key = url || `key:${row.toolkit || row.name || ""}`;
    if ((!url && !row.needs_credentials) || seen.has(key)) return;
    seen.add(key);
    out.push({
      toolkit: row.toolkit || "",
      name: row.name || row.toolkit || "Connect app",
      logo: row.logo || "",
      description: row.description || "",
      redirect_url: url,
      needs_credentials: Boolean(row.needs_credentials),
      fields: Array.isArray(row.fields) ? row.fields : [],
      agent_id: row.agent_id || "",
      agent_name: row.agent_name || "",
    });
  };
  for (const row of asCards(meta?.connect_cards)) add(row);
  add(meta?.source?.snippet);
  add(meta?.source);
  return out;
}

function ConnectPanel({
  card,
  agentId,
  agentName,
}: {
  card: ConnectCardView;
  agentId?: string | null;
  agentName?: string | null;
}) {
  const href = card.redirect_url || "";
  const title = card.name || card.toolkit || "Connect app";
  const forId = card.agent_id || agentId || "";
  const forName = card.agent_name || agentName || "";
  const [busy, setBusy] = useState(false);
  const [enabled, setEnabled] = useState(false);
  const [error, setError] = useState("");
  const [apiKey, setApiKey] = useState("");
  const needsKey = Boolean(card.needs_credentials) && !href;

  async function enable() {
    setBusy(true);
    setError("");
    try {
      if (needsKey && card.toolkit) {
        const field = (card.fields && card.fields[0]?.name) || "generic_api_key";
        const data = await connectMarketplace(card.toolkit, forId || undefined, { [field]: apiKey.trim() });
        if (!data.connected && !data.redirect_url) throw new Error("Could not save that API key");
        if (forId) await grantToolkitAgent(card.toolkit, forId);
        setEnabled(true);
        return;
      }
      if (card.toolkit && forId) {
        await grantToolkitAgent(card.toolkit, forId);
        setEnabled(true);
      }
      if (href) window.open(href, "_blank", "noreferrer");
    } catch (err: any) {
      setError(err.message || "Could not enable this connector");
      if (href) window.open(href, "_blank", "noreferrer");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="connect-card">
      <header className="connect-card-head">
        {card.logo ? <img className="connect-card-logo" src={card.logo} alt="" /> : <span className="connect-card-mark" />}
        <div className="connect-card-copy">
          <strong>{title}</strong>
          <p>
            {needsKey
              ? card.description || "This app uses an API key, not OAuth. Paste the key to enable it."
              : card.description || "Authorize this app in Composio to use it from chat."}
            {forName ? ` Enable it for ${forName}.` : ""}
          </p>
          {needsKey ? (
            <label className="connect-card-key">
              {(card.fields && card.fields[0]?.label) || "API key"}
              <input
                type="password"
                autoComplete="off"
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                placeholder="Paste API key"
              />
            </label>
          ) : null}
          {enabled ? <p className="ok">Enabled{forName ? ` for ${forName}` : ""}.</p> : null}
          {error ? <p className="err">{error}</p> : null}
        </div>
        {href || needsKey ? (
          <button className="btn" type="button" disabled={busy || (needsKey && !apiKey.trim())} onClick={() => void enable()}>
            {busy ? "Enabling…" : enabled ? "Reconnect" : needsKey ? "Save key" : forName ? `Enable for ${forName}` : "Connect"}
          </button>
        ) : null}
      </header>
      {href ? (
        <details className="connect-card-embed">
          <summary>Composio authorization</summary>
          <div className="browser-frame connect-card-browser">
            <div className="browser-chrome">
              <span className="browser-dots" aria-hidden>
                <i />
                <i />
                <i />
              </span>
              <div className="browser-url">{href}</div>
              <a className="browser-open" href={href} target="_blank" rel="noreferrer">
                Open tab
              </a>
            </div>
            <div className="browser-stage">
              <iframe
                className="browser-iframe"
                title={title}
                src={href}
                allow="clipboard-write"
                sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox"
              />
              <a className="connect-card-fallback" href={href} target="_blank" rel="noreferrer">
                If this page is blank, continue in Composio
              </a>
            </div>
          </div>
        </details>
      ) : null}
    </div>
  );
}

export function ConnectCards({
  cards,
  meta,
  agentId,
  agentName,
}: {
  cards?: ConnectCardView[];
  meta?: any;
  agentId?: string | null;
  agentName?: string | null;
}) {
  const list = cardsFromMeta(meta || { connect_cards: cards }).map((card) => ({
    ...card,
    agent_id: card.agent_id || agentId || "",
    agent_name: card.agent_name || agentName || "",
  }));
  if (!list.length) return null;
  return (
    <div className="connect-card-list">
      {list.map((card, i) => (
        <ConnectPanel
          key={`${card.toolkit || card.redirect_url}-${i}`}
          card={card}
          agentId={agentId}
          agentName={agentName}
        />
      ))}
    </div>
  );
}
