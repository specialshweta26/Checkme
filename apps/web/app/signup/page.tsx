"use client";

import { useEffect, useState, type FormEvent } from "react";
import Link from "next/link";
import { AuthProviders } from "../components/AuthProviders";
import { AuthShell, authStatus, postJSON, type AuthStatus } from "../components/AuthShell";
import { getMe } from "../lib";

export default function Signup() {
  const [status, setStatus] = useState<AuthStatus | null>(null);
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [sent, setSent] = useState(false);

  useEffect(() => {
    getMe().then((me) => {
      if (me) window.location.replace("/chat");
    });
    authStatus().then(setStatus);
  }, []);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    const r = await postJSON("/api/auth/email/signup", { name: name.trim(), email: email.trim(), password });
    setBusy(false);
    if (r.ok) setSent(true);
    else setError(r.error);
  }

  if (sent) {
    return (
      <AuthShell eyebrow="Almost there" title="Check your email">
        <p className="auth-intro">
          We sent a link to <strong>{email.trim()}</strong>. Open it to confirm your address and finish creating your account. The
          link works for 24 hours.
        </p>
        <p className="auth-alt">
          Wrong address? <button className="auth-link link-button" type="button" onClick={() => setSent(false)}>Start again</button>
        </p>
        <p className="auth-alt">
          <Link href="/login">Back to sign in</Link>
        </p>
      </AuthShell>
    );
  }

  return (
    <AuthShell eyebrow="Get started" title="Create your Lumio account" intro="Sign up with Google, Microsoft, your company's SSO, or your email.">
      {status?.signup === "closed" ? (
        <p className="auth-intro">
          Lumio isn&apos;t open for new sign-ups yet. If you already have an account, <Link href="/login">sign in</Link>.
        </p>
      ) : null}
      {status?.signup === "invite" ? <p className="auth-alt">Sign-ups are invite-only for now: use the address you were invited with.</p> : null}
      {status?.signup === "closed" ? null : <AuthProviders sso={Boolean(status?.sso)} />}
      {status?.signup === "closed" ? null : status && !status.email ? (
        <p className="auth-alt">Email sign-up isn't available on this server yet. Use one of the options above.</p>
      ) : (
        <>
          <div className="auth-divider" role="separator">
            or with email
          </div>
          <form className="auth-fields" onSubmit={submit}>
            {error ? <p className="err" role="alert">{error}</p> : null}
            <label>
              <span>Name</span>
              <input autoComplete="name" value={name} onChange={(e) => setName(e.target.value)} />
            </label>
            <label>
              <span>Email</span>
              <input type="email" autoComplete="email" required value={email} onChange={(e) => setEmail(e.target.value)} />
            </label>
            <label>
              <span>Password</span>
              <input type="password" autoComplete="new-password" required minLength={10} value={password} onChange={(e) => setPassword(e.target.value)} />
              <small className="muted">At least 10 characters. A short phrase is easiest to remember.</small>
            </label>
            <button className="btn large block" type="submit" disabled={busy}>
              {busy ? "Creating your account…" : "Create account"}
            </button>
          </form>
        </>
      )}
      <p className="auth-alt">
        Already have an account? <Link href="/login">Sign in</Link>
      </p>
    </AuthShell>
  );
}
