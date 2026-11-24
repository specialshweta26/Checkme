"use client";

import { useEffect, useState } from "react";
import { AppShell } from "../components/AppShell";
import { CapsLine, ReasoningSelect, TierSelect, capsText, type Caps, type Tier } from "../components/ModelCaps";
import { IconLock, IconModel, IconPlus } from "../components/ProfileIcons";
import { apiError, getMe, loadWorkspace, relativeTime, type Me, type WorkspaceAgent } from "../lib";

// Models and keys (apps/api/app/model_keys, docs/LLD.md "Model gateway"): the user's own model keys and the model
// their agents use. A key is checked with the provider when it is saved and is never sent back to the page: only its
// provider, name and last four characters. Agents run on any provider: OpenAI natively, Anthropic natively, the others
// on their OpenAI-compatible address.

type Provider = { key: string; name: string; runs: boolean; hint: string; custom_url: boolean };
type SavedKey = {
  id: string;
  provider: string;
  provider_name: string;
  runs: boolean;
  label: string;
  hint: string;
  base_url: string;
  status: "ok" | "failed" | "unchecked";
  status_note: string;
  models: string[];
  caps: Record<string, Caps>;
  checked_at: string | null;
};
type Choice = { key_id: string; model: string; tier?: string; reasoning?: string };
type Overview = {
  providers: Provider[];
  keys: SavedKey[];
  choices: Partial<Record<"agent" | "small", Choice>>;
  agents: Record<string, Partial<Record<"agent" | "small", Choice>>>;
  lumio: { agent: string; small: string };
  tiers: Tier[];
  reasoning?: Tier[];
};
type Form = { provider: string; api_key: string; label: string; base_url: string };
type Draft = { key_id: string; model: string; tier: string; reasoning: string };

const PURPOSES: { key: "agent" | "small"; title: string; body: string }[] = [
  { key: "agent", title: "Agent replies", body: "The model that reads your request, uses tools and writes the answer." },
  { key: "small", title: "Background work", body: "Routing, checks and memory. If you leave it on Lumio's, it uses your agent model when that's your key." },
];

async function call(path: string, init?: RequestInit) {
  const res = await fetch(path, { credentials: "include", headers: { "Content-Type": "application/json" }, ...init });
  if (res.status === 401) {
    window.location.replace("/login");
    throw new Error("Signed out");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(apiError(data, "That didn't work."));
  return data;
}

export default function ModelsPage() {
  const [me, setMe] = useState<Me | null>(null);
  const [data, setData] = useState<Overview | null>(null);
  const [form, setForm] = useState<Form | null>(null);
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [agents, setAgents] = useState<WorkspaceAgent[]>([]);

  function show(next: Overview) {
    setData(next);
    const d: Record<string, Draft> = {};
    for (const p of PURPOSES)
      d[p.key] = {
        key_id: next.choices[p.key]?.key_id || "",
        model: next.choices[p.key]?.model || "",
        tier: next.choices[p.key]?.tier || "",
        reasoning: next.choices[p.key]?.reasoning || "",
      };
    setDrafts(d);
  }

  async function refresh() {
    try {
      show(await call("/api/models"));
    } catch (err: any) {
      setError(err.message);
    }
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
    if (!form) return;
    const close = (e: KeyboardEvent) => e.key === "Escape" && setForm(null);
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, [form]);

  async function run(work: () => Promise<void>) {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await work();
    } catch (err: any) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  const providers = data?.providers || [];
  const keys = data?.keys || [];
  const usable = keys.filter((k) => k.runs && k.status === "ok");
  const picked = form ? providers.find((p) => p.key === form.provider) : undefined;
  const keyName = (id: string) => keys.find((k) => k.id === id)?.label || "";

  const addKey = () =>
    run(async () => {
      if (!form) return;
      const saved: SavedKey = await call("/api/models/keys", { method: "POST", body: JSON.stringify(form) });
      setForm(null);
      await refresh();
      setNotice(
        saved.runs
          ? `${saved.label} works: ${saved.models.length} models available. Choose one below for your agents.`
          : `${saved.label} works and is saved. Lumio can't run agents on ${saved.provider_name} yet; it will be offered here when it can.`,
      );
    });

  const choose = (purpose: "agent" | "small") =>
    run(async () => {
      const d = drafts[purpose];
      const key = keys.find((k) => k.id === d.key_id);
      show(
        await call(`/api/models/choices/${purpose}`, {
          method: "PUT",
          body: JSON.stringify({ key_id: d.key_id || null, model: d.model, tier: key?.provider === "openai" ? d.tier : "", reasoning: d.reasoning }),
        }),
      );
      const thinking = d.reasoning ? ` with ${d.reasoning === "none" ? "no extra" : d.reasoning} reasoning` : "";
      setNotice(
        d.key_id
          ? `Your agents now use ${d.model} on ${keyName(d.key_id)}${thinking} for ${purpose === "agent" ? "replies" : "background work"}.`
          : `On Lumio's model${thinking || " with its default reasoning"}.`,
      );
    });

  const recheck = (k: SavedKey) =>
    run(async () => {
      const row: SavedKey = await call(`/api/models/keys/${k.id}/check`, { method: "POST" });
      await refresh();
      if (row.status === "ok") setNotice(`${row.label} works.`);
      else setError(row.status_note || `${row.label} failed its check.`);
    });

  const remove = (k: SavedKey) => {
    if (!window.confirm(`Remove ${k.label}? Agents using it go back to Lumio's model.`)) return;
    run(async () => {
      show(await call(`/api/models/keys/${k.id}`, { method: "DELETE" }));
      setNotice(`Removed ${k.label}.`);
    });
  };

  return (
    <AppShell me={me} current="models">
      <div className="page">
        <div className="tasks-page">
          <section className="vault-hero">
            <span className="vault-mark" aria-hidden>
              <IconModel size={28} />
            </span>
            <div className="vault-hero-copy">
              <span className="eyebrow">Profile</span>
              <h1>Models and keys</h1>
              <p>Bring your own key from OpenAI, Anthropic, Google, xAI, Groq, OpenRouter or any OpenAI-compatible service, and choose the model your agents use.</p>
            </div>
            <button className="btn large" type="button" onClick={() => { setNotice(""); setForm({ provider: "openai", api_key: "", label: "", base_url: "" }); }}>
              <IconPlus size={16} /> Add key
            </button>
          </section>

          {error ? <p className="err">{error}</p> : null}
          {notice ? <p className="notice">{notice}</p> : null}

          <div className="vault-list-head">
            <h2>Model your agents use</h2>
          </div>
          <div className="vault-list models-choices">
            {PURPOSES.map((p) => {
              const d = drafts[p.key] || { key_id: "", model: "", tier: "", reasoning: "" };
              const key = keys.find((k) => k.id === d.key_id);
              const current = data?.choices[p.key];
              const changed =
                (current?.key_id || "") !== d.key_id || (current?.model || "") !== d.model || (current?.tier || "") !== d.tier || (current?.reasoning || "") !== d.reasoning;
              const caps = key && d.model ? key.caps?.[d.model] : undefined;
              const blocked = p.key === "agent" && caps?.tools === false;
              return (
                <div key={p.key} className="models-choice">
                  <div className="vault-item-main">
                    <strong>{p.title}</strong>
                    <span className="vault-item-meta">{p.body}</span>
                  </div>
                  <label className="vault-field">
                    <span>Key</span>
                    <select
                      value={d.key_id}
                      disabled={busy}
                      onChange={(e) => {
                        const k = keys.find((x) => x.id === e.target.value);
                        setDrafts({ ...drafts, [p.key]: { key_id: e.target.value, model: k?.models.includes(d.model) ? d.model : "", tier: k?.provider === "openai" ? d.tier : "", reasoning: d.reasoning } });
                      }}
                    >
                      <option value="">Lumio's ({data?.lumio[p.key] || "default"})</option>
                      {usable.map((k) => (
                        <option key={k.id} value={k.id}>
                          {k.label} ••••{k.hint}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="vault-field">
                    <span>Model</span>
                    <input
                      list={`models-${p.key}`}
                      disabled={busy || !d.key_id}
                      placeholder={d.key_id ? "Pick or type a model" : data?.lumio[p.key] || ""}
                      value={d.key_id ? d.model : ""}
                      onChange={(e) => setDrafts({ ...drafts, [p.key]: { ...d, model: e.target.value } })}
                    />
                    <datalist id={`models-${p.key}`}>
                      {(key?.models || []).map((m) => (
                        <option key={m} value={m} label={capsText(key?.caps?.[m])} />
                      ))}
                    </datalist>
                    {d.key_id ? <CapsLine caps={caps} model={d.model} /> : null}
                  </label>
                  <label className="vault-field">
                    <span>Speed</span>
                    {key?.provider === "openai" ? (
                      <TierSelect tiers={data?.tiers || []} value={d.tier} disabled={busy} onChange={(tier) => setDrafts({ ...drafts, [p.key]: { ...d, tier } })} />
                    ) : (
                      <em className="models-caps">{d.key_id ? "Set by the provider." : "Standard."}</em>
                    )}
                  </label>
                  <label className="vault-field">
                    <span>Reasoning</span>
                    <ReasoningSelect levels={data?.reasoning || []} value={d.reasoning} disabled={busy} onChange={(reasoning) => setDrafts({ ...drafts, [p.key]: { ...d, reasoning } })} />
                  </label>
                  <button className="btn small" type="button" disabled={busy || !changed || blocked || (!!d.key_id && !d.model.trim())} onClick={() => choose(p.key)}>
                    Save
                  </button>
                </div>
              );
            })}
          </div>
          <p className="faint models-note">
            Calls on your key are billed by your provider. They show in Usage as "model (your key)" and don't count toward Lumio's daily limit.
            {usable.length === 0 ? " Add a key to choose a model." : ""} Your model needs tool calling to work as an agent; image attachments need a model that reads images.
          </p>

          {agents.length ? (
            <>
              <div className="vault-list-head">
                <h2>Each agent</h2>
              </div>
              <div className="vault-list">
                {agents.map((a) => {
                  const own = data?.agents?.[a.id]?.agent;
                  return (
                    <div key={a.id} className="vault-item models-agent">
                      <span className="vault-monogram" aria-hidden>
                        {a.name.slice(0, 1).toUpperCase()}
                      </span>
                      <div className="vault-item-main">
                        <strong>{a.name}</strong>
                        <span className="vault-item-meta">
                          {own ? (
                            <span>
                              Its own model: {own.model} on {keyName(own.key_id) || "your key"}
                            </span>
                          ) : (
                            <span>Same as your default</span>
                          )}
                        </span>
                      </div>
                      <a className="btn secondary small" href={`/models?about=${a.id}`}>
                        Change
                      </a>
                    </div>
                  );
                })}
              </div>
              <p className="faint models-note">Change opens the agent's About panel: choose its key and model under Model. The list updates when you reload the page.</p>
            </>
          ) : null}

          <div className="vault-list-head">
            <h2>
              Saved keys <span className="pill-count">{keys.length}</span>
            </h2>
          </div>
          {data && keys.length === 0 ? (
            <div className="task-empty showcase-empty">
              <span className="showcase-icon tone-vault">
                <IconLock size={24} />
              </span>
              <strong>No keys yet</strong>
              <p className="muted">Your agents use Lumio's models. Add a key to use your own account and models instead.</p>
              <button className="btn" type="button" onClick={() => setForm({ provider: "openai", api_key: "", label: "", base_url: "" })}>
                Add your first key
              </button>
            </div>
          ) : null}
          <div className="vault-list">
            {keys.map((k) => (
              <div key={k.id} className="vault-item">
                <span className="vault-monogram" aria-hidden>
                  {k.provider_name.slice(0, 1).toUpperCase()}
                </span>
                <div className="vault-item-main">
                  <strong>{k.label}</strong>
                  <span className="vault-item-meta">
                    <span>{k.provider_name}</span>
                    <span className="vault-secret">••••{k.hint}</span>
                    {k.base_url ? <span>{k.base_url}</span> : null}
                    <span className={`models-status ${k.status}`}>{k.status === "ok" ? `Works · ${k.models.length} models` : k.status === "failed" ? "Failed" : "Not checked"}</span>
                    {!k.runs ? <span className="faint">Agents can't use {k.provider_name} yet</span> : null}
                    {k.status === "failed" && k.status_note ? <span className="err-inline">{k.status_note}</span> : null}
                  </span>
                </div>
                <span className="vault-used">{k.checked_at ? `Checked ${relativeTime(k.checked_at)}` : ""}</span>
                <div className="vault-actions">
                  <button className="btn secondary small" type="button" disabled={busy} onClick={() => recheck(k)}>
                    Check again
                  </button>
                  <button className="btn secondary small" type="button" disabled={busy} onClick={() => remove(k)} aria-label={`Remove ${k.label}`}>
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
            aria-labelledby="models-form-title"
            autoComplete="off"
            onClick={(e) => e.stopPropagation()}
            onSubmit={(e) => {
              e.preventDefault();
              addKey();
            }}
          >
            <div className="vault-form-head">
              <span className="vault-promise-icon">
                <IconLock size={16} />
              </span>
              <div>
                <strong id="models-form-title">Add a model key</strong>
                <span className="faint">Lumio checks it with the provider, then encrypts it. You won't see it again.</span>
              </div>
            </div>
            <label className="vault-field">
              <span>Provider</span>
              <select value={form.provider} onChange={(e) => setForm({ ...form, provider: e.target.value })}>
                {providers.map((p) => (
                  <option key={p.key} value={p.key}>
                    {p.name}
                    {p.runs ? "" : " (saved for later)"}
                  </option>
                ))}
              </select>
              {picked ? <em>{picked.hint}</em> : null}
            </label>
            {picked?.custom_url ? (
              <label className="vault-field">
                <span>API address</span>
                <input required placeholder="https://api.mistral.ai/v1" value={form.base_url} onChange={(e) => setForm({ ...form, base_url: e.target.value })} />
              </label>
            ) : null}
            <label className="vault-field">
              <span>API key</span>
              <input required autoFocus type="password" autoComplete="new-password" value={form.api_key} onChange={(e) => setForm({ ...form, api_key: e.target.value })} />
            </label>
            <label className="vault-field">
              <span>
                Name <em>optional</em>
              </span>
              <input placeholder={picked ? `My ${picked.name} key` : ""} value={form.label} onChange={(e) => setForm({ ...form, label: e.target.value })} />
            </label>
            <div className="row vault-form-actions">
              <button type="button" className="btn secondary" onClick={() => setForm(null)}>
                Cancel
              </button>
              <button className="btn" disabled={busy || !form.api_key.trim() || (!!picked?.custom_url && !form.base_url.trim())}>
                {busy ? "Checking…" : "Check and save"}
              </button>
            </div>
          </form>
        </div>
      ) : null}
    </AppShell>
  );
}
