"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { BrandMark } from "../components/BrandMark";
import { AuthProviders } from "../components/AuthProviders";
import { applySavedTheme, devLoginAvailable, errorFromQuery, getMe } from "../lib";

export default function Login() {
  const [error, setError] = useState("");
  const [deleted, setDeleted] = useState(false);
  const [devLogin, setDevLogin] = useState(false);

  useEffect(() => applySavedTheme(), []);

  useEffect(() => {
    setError(errorFromQuery(window.location.search));
    setDeleted(new URLSearchParams(window.location.search).get("deleted") === "1");
    getMe().then((me) => {
      if (me) window.location.replace("/chat");
    });
    devLoginAvailable().then(setDevLogin);
  }, []);

  return (
    <div className="auth">
      <main className="auth-shell">
        <section className="auth-visual" aria-label="Lumio">
          <Link className="auth-brand" href="/">
            <span className="auth-mark">
              <BrandMark size={32} />
            </span>
            Lumio
          </Link>
          <div className="auth-hero-copy">
            <h1>Your assistant for the work that fills your day.</h1>
            <p>Lumio reads your inbox, runs your routines and works across the apps you connect, with your approval before anything goes out.</p>
            <ul className="auth-points">
              <li>Agents with their own skills, memory and schedule</li>
              <li>Approvals before any email, message or payment is sent</li>
              <li>Your data stays in your workspace, exportable at any time</li>
            </ul>
          </div>
          <p className="auth-foot">Private to your accounts. Nothing is shared between workspaces.</p>
        </section>
        <section className="auth-action">
          <div className="auth-form">
            <div className="auth-eyebrow">Welcome back</div>
            <h2>Sign in to Lumio</h2>
            <p className="auth-intro">Use the work or personal account you connect your mail and calendar with.</p>
            {deleted ? <p className="notice">Your account and all its data were deleted.</p> : null}
            <AuthProviders error={error} />
            {devLogin ? (
              <>
                <div className="auth-divider" role="separator">
                  Local development
                </div>
                <form className="dev-login" method="post" action="/api/auth/dev/login">
                  <button className="btn secondary large block" type="submit">
                    Continue with local demo account
                  </button>
                  <p>Shown only when this server runs on your machine with DEV_LOGIN on. It never appears in production.</p>
                </form>
              </>
            ) : null}
            <p className="auth-alt">
              <Link href="/download">Download Lumio for Mac or Windows</Link>
            </p>
            <details className="trouble">
              <summary>Having trouble signing in?</summary>
              <p>
                Google: if the Cloud app is in Testing, add your account under Auth Platform → Audience → Test users.
                Outlook: the Azure Application (client) ID must be a full GUID, and supported account types must include
                personal Microsoft accounts.
              </p>
            </details>
            <p className="trust">By signing in, you agree this is a private workspace for your accounts.</p>
          </div>
        </section>
      </main>
    </div>
  );
}
