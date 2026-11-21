"use client";

import { useState, type FormEvent } from "react";
import Link from "next/link";
import { postJSON } from "./AuthShell";

/** Email and password sign-in. Works inside the desktop app too: the session cookie lands in its own window. */
export function EmailSignIn() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [unverified, setUnverified] = useState(false);
  const [notice, setNotice] = useState("");

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    setNotice("");
    const r = await postJSON("/api/auth/email/login", { email: email.trim(), password });
    setBusy(false);
    if (r.ok) {
      window.location.replace(r.data.redirect || "/chat");
      return;
    }
    setUnverified(r.status === 403);
    setError(r.error);
  }

  async function resend() {
    const r = await postJSON("/api/auth/email/resend", { email: email.trim() });
    setUnverified(false);
    setError("");
    setNotice(r.ok ? "We sent you a new confirmation link." : r.error);
  }

  return (
    <form className="auth-fields" onSubmit={submit}>
      {error ? <p className="err" role="alert">{error}</p> : null}
      {notice ? <p className="notice">{notice}</p> : null}
      {unverified ? (
        <button className="btn secondary small" type="button" onClick={() => void resend()}>
          Send the confirmation link again
        </button>
      ) : null}
      <label>
        <span>Email</span>
        <input type="email" autoComplete="email" required value={email} onChange={(e) => setEmail(e.target.value)} />
      </label>
      <label>
        <span className="auth-label-row">
          Password
          <Link className="auth-link" href="/forgot-password">
            Forgot password?
          </Link>
        </span>
        <input type="password" autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} />
      </label>
      <button className="btn large block" type="submit" disabled={busy}>
        {busy ? "Signing in…" : "Sign in"}
      </button>
      <p className="auth-alt">
        New to Lumio? <Link href="/signup">Create an account</Link>
      </p>
    </form>
  );
}
