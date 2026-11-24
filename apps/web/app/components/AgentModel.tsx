"use client";

import { useEffect, useState } from "react";
import { apiError, type WorkspaceAgent } from "../lib";
import { CapsLine, ReasoningSelect, TierSelect, capsText, type Caps, type Tier } from "./ModelCaps";

// The agent's own model (docs/LLD.md "Model gateway", phase 3), in its About panel: one of the user's saved keys and a
// model on it, or "Same as your default" (the Models page choice, else Lumio's). Keys are added on /models.

type Key = { id: string; provider: string; label: string; hint: string; runs: boolean; status: string; models: string[]; caps: Record<string, Caps> };
type Choice = { key_id: string; model: string; tier?: string; reasoning?: string };
type Overview = { keys: Key[]; choices: { agent?: Choice }; agents: Record<string, { agent?: Choice }>; lumio: { agent: string }; tiers: Tier[]; reasoning?: Tier[] };

export function AgentModel({ agent }: { agent: WorkspaceAgent }) {
  const [data, setData] = useState<Overview | null>(null);
  const [draft, setDraft] = useState<Choice>({ key_id: "", model: "", tier: "", reasoning: "" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  function fill(d: Overview) {
    setData(d);
    const own = d.agents?.[agent.id]?.agent;
    setDraft({ key_id: own?.key_id || "", model: own?.model || "", tier: own?.tier || "", reasoning: own?.reasoning || "" });
  }

  useEffect(() => {
    setData(null);
    setError("");
    setNotice("");
    fetch("/api/models", { credentials: "include" })
      .then(async (r) => {
        const d = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(apiError(d, "Couldn't load models."));
        fill(d);
      })
      .catch((e) => setError(e.message));
  }, [agent.id]);

  const keys = (data?.keys || []).filter((k) => k.runs && k.status === "ok");
  const key = keys.find((k) => k.id === draft.key_id);
  const own = data?.agents?.[agent.id]?.agent;
  const fallback = data?.choices?.agent;
  const fallbackName = fallback ? `${fallback.model} on ${data?.keys.find((k) => k.id === fallback.key_id)?.label || "your key"}` : `Lumio's ${data?.lumio.agent || "model"}`;
  const dirty =
    (own?.key_id || "") !== draft.key_id ||
    (own?.model || "") !== draft.model ||
    (own?.tier || "") !== (draft.tier || "") ||
    (own?.reasoning || "") !== (draft.reasoning || "");
  const caps = key && draft.model ? key.caps?.[draft.model] : undefined;
  const blocked = caps?.tools === false;

  async function save() {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const res = await fetch("/api/models/choices/agent", {
        method: "PUT",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          key_id: draft.key_id || null,
          model: draft.model,
          agent_id: agent.id,
          tier: key?.provider === "openai" ? draft.tier || "" : "",
          reasoning: draft.reasoning || "",
        }),
      });
      const d = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(apiError(d, "Couldn't save the model."));
      fill(d);
      setNotice(draft.key_id ? `${agent.name} now uses ${draft.model}.` : `${agent.name} uses your default again.`);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="limits agent-model">
      <p className="about-label">Model</p>
      <form
        className="limits-form"
        onSubmit={(e) => {
          e.preventDefault();
          void save();
        }}
      >
        <label>
          <span>Key</span>
          <span className="limits-input">
            <select
              aria-label={`Key ${agent.name} uses`}
              value={draft.key_id}
              disabled={busy || !data}
              onChange={(e) => {
                const k = keys.find((x) => x.id === e.target.value);
                setDraft({ key_id: e.target.value, model: k?.models.includes(draft.model) ? draft.model : "", tier: k?.provider === "openai" ? draft.tier : "" });
              }}
            >
              <option value="">Same as your default</option>
              {keys.map((k) => (
                <option key={k.id} value={k.id}>
                  {k.label} ••••{k.hint}
                </option>
              ))}
            </select>
          </span>
        </label>
        <label>
          <span>Model</span>
          <span className="limits-input">
            <input
              list={`agent-models-${agent.id}`}
              aria-label={`Model ${agent.name} uses`}
              disabled={busy || !draft.key_id}
              placeholder={draft.key_id ? "Pick or type a model" : fallbackName}
              value={draft.key_id ? draft.model : ""}
              onChange={(e) => setDraft({ ...draft, model: e.target.value })}
            />
            <datalist id={`agent-models-${agent.id}`}>
              {(key?.models || []).map((m) => (
                <option key={m} value={m} label={capsText(key?.caps?.[m])} />
              ))}
            </datalist>
          </span>
          {draft.key_id ? <CapsLine caps={caps} model={draft.model} /> : null}
        </label>
        {key?.provider === "openai" ? (
          <label>
            <span>Speed</span>
            <span className="limits-input">
              <TierSelect tiers={data?.tiers || []} value={draft.tier || ""} disabled={busy} onChange={(tier) => setDraft({ ...draft, tier })} />
            </span>
          </label>
        ) : null}
        <label>
          <span>Reasoning</span>
          <span className="limits-input">
            <ReasoningSelect levels={data?.reasoning || []} value={draft.reasoning || ""} disabled={busy} onChange={(reasoning) => setDraft({ ...draft, reasoning })} />
          </span>
        </label>
        <button className="btn small" type="submit" disabled={busy || !dirty || blocked || (!!draft.key_id && !draft.model.trim())}>
          {busy ? "Saving…" : "Save model"}
        </button>
      </form>
      {!draft.key_id && data ? <p className="faint about-note">Uses {fallbackName}.</p> : null}
      {data && keys.length === 0 ? (
        <p className="faint about-note">
          <a href="/models">Add a model key</a> to give {agent.name} its own model.
        </p>
      ) : (
        <p className="faint about-note">
          <a href="/models">Manage keys</a>
        </p>
      )}
      {error ? <p className="err about-note">{error}</p> : null}
      {notice ? <p className="faint about-note">{notice}</p> : null}
    </div>
  );
}
