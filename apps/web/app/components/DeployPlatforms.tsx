"use client";

/**
 * Deploy platform credentials for the Coder's CLI deploys (Render, Vercel, Fly.io, Netlify, Railway,
 * AWS, Google Cloud, Azure). Values are sent once to `PUT /api/connectors/deploy/{platform}`, stored
 * encrypted, and never shown again, to you or to the agent. Every deploy and rollback still waits
 * for your approval in chat.
 */
import { useEffect, useState } from "react";

type Field = { env: string; label: string; required: boolean };
type DeployPlatform = {
  key: string;
  name: string;
  cli: string;
  token_url: string;
  notes: string;
  fields: Field[];
  saved: string[];
  connected: boolean;
};

export function DeployPlatforms() {
  const [platforms, setPlatforms] = useState<DeployPlatform[]>([]);
  const [open, setOpen] = useState("");
  const [values, setValues] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState("");
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");

  async function load() {
    const res = await fetch("/api/connectors/deploy", { credentials: "include" });
    if (res.ok) setPlatforms((await res.json()).platforms || []);
  }

  useEffect(() => {
    load();
    const want = new URLSearchParams(window.location.search).get("deploy");
    if (want && want !== "all") setOpen(want);
    if (want) setTimeout(() => document.getElementById("deploy-platforms")?.scrollIntoView({ behavior: "smooth" }), 300);
  }, []);

  async function save(p: DeployPlatform) {
    setBusy(p.key);
    setErr("");
    setMsg("");
    try {
      const res = await fetch(`/api/connectors/deploy/${p.key}`, {
        method: "PUT",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ values }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Could not save");
      setValues({});
      setOpen("");
      setMsg(`${p.name} key saved. Your Coder uses it instead of a sign-in link; each deploy asks for your approval.`);
      await load();
    } catch (e: any) {
      setErr(e.message || "Could not save");
    } finally {
      setBusy("");
    }
  }

  async function forget(p: DeployPlatform) {
    setBusy(p.key);
    await fetch(`/api/connectors/deploy/${p.key}`, { method: "DELETE", credentials: "include" });
    setBusy("");
    await load();
  }

  return (
    <div id="deploy-platforms" className="card stack" style={{ marginBottom: 18 }}>
      <header className="row" style={{ justifyContent: "space-between" }}>
        <strong>Deploy platforms</strong>
        <span className="badge ok">
          {platforms.filter((p) => p.connected).length ? `${platforms.filter((p) => p.connected).length} keys saved` : "Ready · keys optional"}
        </span>
      </header>
      <p className="muted" style={{ margin: 0 }}>
        Nothing to set up: your Coder installs each platform&apos;s CLI on its computer and, the first time, sends you one
        sign-in link in chat; the session stays on the computer. Saving a key here is an optional shortcut (stored
        encrypted, never shown again, to you or to the agent). Every deploy and rollback waits for your approval. A
        platform&apos;s MCP connector, if you add one, is used first.
      </p>
      {msg ? <p className="ok" style={{ margin: 0 }}>{msg}</p> : null}
      {err ? <p className="err" style={{ margin: 0 }}>{err}</p> : null}
      {platforms.map((p) => (
        <div key={p.key} className="stack" style={{ gap: 6 }}>
          <div className="row" style={{ justifyContent: "space-between" }}>
            <span>
              <strong>{p.name}</strong> <span className="muted">· {p.cli} CLI</span>
              {p.connected ? <span className="badge ok" style={{ marginLeft: 8 }}>connected</span> : null}
            </span>
            <span className="row">
              <button className="btn secondary" type="button" onClick={() => { setOpen(open === p.key ? "" : p.key); setValues({}); }}>
                {p.connected ? "Update key" : "Add key (optional)"}
              </button>
              {p.connected ? (
                <button className="btn secondary" type="button" disabled={busy === p.key} onClick={() => forget(p)}>
                  Forget
                </button>
              ) : null}
            </span>
          </div>
          {open === p.key ? (
            <form
              className="stack"
              style={{ gap: 6 }}
              autoComplete="off"
              onSubmit={(e) => {
                e.preventDefault();
                save(p);
              }}
            >
              {p.fields.map((f) => (
                <label key={f.env} className="stack" style={{ gap: 2 }}>
                  <span className="muted" style={{ fontSize: 12 }}>
                    {f.env} · {f.label}
                    {p.saved.includes(f.env) ? " (saved; leave blank to keep)" : ""}
                  </span>
                  {f.env.endsWith("_JSON") ? (
                    <textarea
                      rows={4}
                      spellCheck={false}
                      value={values[f.env] || ""}
                      onChange={(e) => setValues({ ...values, [f.env]: e.target.value })}
                    />
                  ) : (
                    <input
                      type={/ID$|REGION$|PROJECT$/.test(f.env) ? "text" : "password"}
                      autoComplete="off"
                      value={values[f.env] || ""}
                      onChange={(e) => setValues({ ...values, [f.env]: e.target.value })}
                    />
                  )}
                </label>
              ))}
              {p.notes ? <p className="muted" style={{ margin: 0, fontSize: 12 }}>{p.notes}</p> : null}
              <div className="row">
                <button className="btn" type="submit" disabled={busy === p.key}>
                  Save
                </button>
                {p.token_url ? (
                  <a className="btn secondary" href={p.token_url} target="_blank" rel="noreferrer">
                    Where do I get this?
                  </a>
                ) : null}
              </div>
            </form>
          ) : null}
        </div>
      ))}
    </div>
  );
}
