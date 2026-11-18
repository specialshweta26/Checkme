"use client";

import { useEffect, useState } from "react";
import { Select } from "./Select";
import { getAgentBrowser, relativeTime, resetAgentBrowser, setAgentBrowser, type AgentBrowserState, type WorkspaceAgent } from "../lib";

// A separate browser per agent (docs/AGENT_BROWSERS.md), in the agent's About panel: whether it shares the cloud
// computer's browser (and your sign-ins there) or has its own, and a way to sign its own browser out of everything.

const MODES: { value: AgentBrowserState["mode"]; label: string }[] = [
  { value: "shared", label: "Shared browser" },
  { value: "own", label: "Its own browser" },
];

export function AgentBrowser({ agent }: { agent: WorkspaceAgent }) {
  const [data, setData] = useState<AgentBrowserState | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  useEffect(() => {
    setData(null);
    setConfirm(false);
    setError("");
    setNotice("");
    getAgentBrowser(agent.id).then(setData).catch((e) => setError(e.message));
  }, [agent.id]);

  async function run(work: () => Promise<AgentBrowserState>, done: string) {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      setData(await work());
      setNotice(done);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
      setConfirm(false);
    }
  }

  const mode = data?.mode || agent.browser?.mode || "shared";

  return (
    <div className="agent-browser">
      <p className="about-label">Browser</p>
      <div className="handoffs-rule">
        <span>Which browser {agent.name} uses</span>
        <Select
          value={mode}
          label={`Which browser ${agent.name} uses`}
          options={MODES}
          disabled={busy || !data}
          onChange={(v) => {
            if (v !== mode) void run(() => setAgentBrowser(agent.id, v as AgentBrowserState["mode"]), "Saved.");
          }}
        />
      </div>
      <p className="faint about-note">
        {mode === "own"
          ? `${agent.name} has its own browser on the cloud computer, with its own cookies and sign-ins. Sites you sign into in the shared browser stay closed to it, and what it signs into stays out of the shared browser.`
          : `${agent.name} uses the shared browser on the cloud computer, so any site you signed into there is open to it, and to every other agent that shares it.`}
      </p>
      {mode === "own" && data ? (
        <div className="agent-browser-own">
          <span className="faint">
            {data.saved_cookies
              ? `Signed-in state saved${data.saved_at ? ` ${relativeTime(data.saved_at)}` : ""}, so it survives a computer restart.`
              : "Nothing saved yet. It starts signed out of every site."}
          </span>
          {confirm ? (
            <span className="agent-browser-confirm" role="group" aria-label="Confirm sign out">
              <span>Sign {agent.name} out of every site? This can’t be undone.</span>
              <button className="btn small danger" type="button" disabled={busy} onClick={() => void run(() => resetAgentBrowser(agent.id), `${agent.name} now starts signed out of every site.`)}>
                {busy ? "Signing out…" : "Sign out"}
              </button>
              <button className="btn small secondary" type="button" disabled={busy} onClick={() => setConfirm(false)}>
                Cancel
              </button>
            </span>
          ) : (
            <button className="btn small secondary" type="button" disabled={busy} onClick={() => setConfirm(true)}>
              Sign out of everything
            </button>
          )}
        </div>
      ) : null}
      {mode === "own" ? (
        <p className="faint about-note">
          In the desktop app it gets its own Chrome window on your computer too. That needs the latest desktop app; an older one refuses its browser steps
          instead of using the shared browser.
        </p>
      ) : null}
      {error ? <p className="err">{error}</p> : null}
      {notice ? <p className="faint about-note">{notice}</p> : null}
    </div>
  );
}
