"use client";

import { useState, type FormEvent } from "react";
import { postJSON } from "./AuthShell";

/** For an account that signs in with email and password: change it (other devices are signed out). */
export function ChangePassword() {
  const [current, setCurrent] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [done, setDone] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    setDone(false);
    const r = await postJSON("/api/auth/email/password", { current, password });
    setBusy(false);
    if (r.ok) {
      setDone(true);
      setCurrent("");
      setPassword("");
    } else setError(r.error);
  }

  return (
    <div className="task-card">
      <form className="task-main stack" onSubmit={submit}>
        <strong>Password</strong>
        <span className="muted">Change the password you sign in with. Your other devices are signed out.</span>
        <label className="faint" htmlFor="pw-current">Current password</label>
        <input id="pw-current" className="field" type="password" autoComplete="current-password" required value={current} onChange={(e) => setCurrent(e.target.value)} />
        <label className="faint" htmlFor="pw-new">New password (at least 10 characters)</label>
        <input id="pw-new" className="field" type="password" autoComplete="new-password" required minLength={10} value={password} onChange={(e) => setPassword(e.target.value)} />
        {error ? <p className="err">{error}</p> : null}
        {done ? <p className="notice">Password changed.</p> : null}
        <div className="row">
          <button className="btn" type="submit" disabled={busy}>
            {busy ? "Saving…" : "Change password"}
          </button>
        </div>
      </form>
    </div>
  );
}
