"use client";

import { useEffect, useState } from "react";
import { AppShell } from "../components/AppShell";
import { apiError, getMe, type Me } from "../lib";

// Your data (apps/api/app/account/routes.py): download everything the app holds about you, or delete your account.

export default function AccountPage() {
  const [me, setMe] = useState<Me | null>(null);
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
    });
  }, []);

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
              <div className="task-actions">
                <a className="btn" href="/api/me/export">
                  Download
                </a>
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
