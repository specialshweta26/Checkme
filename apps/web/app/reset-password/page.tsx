"use client";

import { useEffect, useState, type FormEvent } from "react";
import Link from "next/link";
import { AuthShell, postJSON, tokenFromQuery } from "../components/AuthShell";

export default function ResetPassword() {
  const [token, setToken] = useState("");
  const [password, setPassword] = useState("");
  const [again, setAgain] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => setToken(tokenFromQuery()), []);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (password !== again) {
      setError("The two passwords don't match.");
      return;
    }
    setBusy(true);
    setError("");
    const r = await postJSON("/api/auth/email/reset", { token, password });
    setBusy(false);
    if (r.ok) window.location.replace(r.data.redirect || "/chat");
    else setError(r.error);
  }

  return (
    <AuthShell eyebrow="Password help" title="Choose a new password" intro="You'll be signed in, and signed out everywhere else.">
      <form className="auth-fields" onSubmit={submit}>
        {error ? <p className="err" role="alert">{error}</p> : null}
        <label>
          <span>New password</span>
          <input type="password" autoComplete="new-password" required minLength={10} autoFocus value={password} onChange={(e) => setPassword(e.target.value)} />
          <small className="muted">At least 10 characters.</small>
        </label>
        <label>
          <span>Type it again</span>
          <input type="password" autoComplete="new-password" required minLength={10} value={again} onChange={(e) => setAgain(e.target.value)} />
        </label>
        <button className="btn large block" type="submit" disabled={busy || !token}>
          {busy ? "Saving…" : "Save and sign in"}
        </button>
      </form>
      <p className="auth-alt">
        Link expired? <Link href="/forgot-password">Get a new one</Link>
      </p>
    </AuthShell>
  );
}
