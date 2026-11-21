"use client";

import { useState, type FormEvent } from "react";
import Link from "next/link";
import { AuthShell, postJSON } from "../components/AuthShell";

export default function ForgotPassword() {
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [sent, setSent] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    const r = await postJSON("/api/auth/email/forgot", { email: email.trim() });
    setBusy(false);
    if (r.ok) setSent(true);
    else setError(r.error);
  }

  return (
    <AuthShell
      eyebrow="Password help"
      title={sent ? "Check your email" : "Reset your password"}
      intro={sent ? undefined : "Enter the email you sign in with. We'll send you a link to choose a new password."}
    >
      {sent ? (
        <p className="auth-intro">
          If <strong>{email.trim()}</strong> has a Lumio password, a reset link is on its way. It works for one hour. If you sign in
          with Google, Microsoft or SSO, the email tells you so instead.
        </p>
      ) : (
        <form className="auth-fields" onSubmit={submit}>
          {error ? <p className="err" role="alert">{error}</p> : null}
          <label>
            <span>Email</span>
            <input type="email" autoComplete="email" required autoFocus value={email} onChange={(e) => setEmail(e.target.value)} />
          </label>
          <button className="btn large block" type="submit" disabled={busy}>
            {busy ? "Sending…" : "Send reset link"}
          </button>
        </form>
      )}
      <p className="auth-alt">
        <Link href="/login">Back to sign in</Link>
      </p>
    </AuthShell>
  );
}
