"use client";

import { useEffect, useState } from "react";
import { AppShell } from "../components/AppShell";
import { apiError, getMe, type Me } from "../lib";

// Your data (apps/api/app/account/routes.py): download everything the app holds about you, or delete your account.

type Export = { state: "none" | "preparing" | "ready" | "failed"; bytes?: number; ready_at?: number; error?: string };

export default function AccountPage() {
  const [me, setMe] = useState<Me | null>(null);
  const [exp, setExp] = useState<Export>({ state: "none" });
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    getMe().then((u) => {
      if (!u) {
        window.location.replace("/login");
        return;
      }
      setMe(u);
      fetch("/api/me/export", { credentials: "include" })
        .then((r) => (r.ok ? r.json() : { state: "none" }))
        .then(setExp)
        .catch(() => undefined);
    });
  }, []);

  // While an export is being prepared, check every few seconds.
  useEffect(() => {
    if (exp.state !== "preparing") return;
    const id = window.setInterval(() => {
      fetch("/api/me/export", { credentials: "include" })
        .then((r) => (r.ok ? r.json() : exp))
        .then(setExp)
        .catch(() => undefined);
    }, 3000);
    return () => window.clearInterval(id);
  }, [exp]);

  async function prepare() {
    const res = await fetch("/api/me/export", { method: "POST", credentials: "include" });
    setExp(res.ok ? await res.json() : { state: "failed", error: "The export could not be started. Please try again." });
  }

  const matches = !!me?.email && confirm.trim().toLowerCase() === me.email.toLowerCase();

  async function remove() {
    setBusy(true);
    setError("");
    const res = await fetch("/api/me/delete", {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      setError(apiError(data, "Your account could not be deleted. Nothing was removed; please try again."));
      setBusy(false);
      return;
    }
    window.location.replace("/login?deleted=1");
  }

  return (
    <AppShell me={me} current="account">
      <div className="page">
        <div className="tasks-page">
          <div className="page-hero">
            <div>
              <h1>Your data</h1>
              <p className="muted">Download everything your assistant holds about you, or delete your account and all of it.</p>
            </div>
          </div>
          <div className="task-list">
            <div className="task-card">
              <div className="task-main">
                <strong>Download your data</strong>
                <span className="muted">
                  A ZIP file with your chats, memory, people, inbox cards, tasks, automations and usage, and the files you uploaded. Your
                  sign-in passwords and keys are never included.
                </span>
              </div>
              <div className="task-actions stack">
                {exp.state === "ready" ? (
                  <>
                    <a className="btn" href="/api/me/export/download">
                      Download ({((exp.bytes || 0) / 1e6).toFixed(1)} MB)
                    </a>
                    <span className="faint">Prepared {exp.ready_at ? new Date(exp.ready_at * 1000).toLocaleString() : ""}</span>
                    <button className="btn secondary" onClick={prepare}>
                      Prepare a new one
                    </button>
                  </>
                ) : exp.state === "preparing" ? (
                  <span className="muted">Preparing your export… This can take a few minutes; you can leave this page and come back.</span>
                ) : (
                  <>
                    <button className="btn" onClick={prepare}>
                      Prepare download
                    </button>
                    {exp.state === "failed" ? <span className="err">{exp.error || "The export failed. Please try again."}</span> : null}
                  </>
                )}
              </div>
            </div>

            <div className="task-card">
              <div className="task-main stack">
                <strong>Delete your account</strong>
                <span className="muted">
                  This permanently deletes your account and everything in it: chats, memory, inbox cards, tasks, automations, files and your
                  cloud computer. Your actual Gmail or Outlook mailbox is not touched. This can't be undone, so download your data first if you
                  might want it.
                </span>
                <label className="faint" htmlFor="confirm-email">
                  To confirm, type your email address{me?.email ? ` (${me.email})` : ""}
                </label>
                <input id="confirm-email" className="field" autoComplete="off" value={confirm} onChange={(e) => setConfirm(e.target.value)} />
                {error ? <p className="err">{error}</p> : null}
                <div className="row">
                  <button className="btn danger" disabled={!matches || busy} onClick={remove}>
                    {busy ? "Deleting…" : "Delete my account permanently"}
                  </button>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </AppShell>
  );
}
