"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { BrandMark } from "../components/BrandMark";
import { AuthProviders } from "../components/AuthProviders";
import { errorFromQuery, getMe } from "../lib";

export default function Login() {
  const [error, setError] = useState("");
  const [deleted, setDeleted] = useState(false);

  useEffect(() => {
    setError(errorFromQuery(window.location.search));
    setDeleted(new URLSearchParams(window.location.search).get("deleted") === "1");
    getMe().then((me) => {
      if (me) window.location.replace("/chat");
    });
  }, []);

  return (
    <div className="auth">
      <main className="auth-shell">
        <section className="auth-visual" aria-label="Lumio">
          <Link className="auth-brand" href="/">
            <span className="auth-mark">
              <BrandMark size={22} />
            </span>
            lumio
          </Link>
          <div className="auth-hero-copy">
            <h1>Your personal assistant, ready when you are.</h1>
            <p>Bring your day into focus. Lumio helps you think, plan, and get things done—one conversation at a time.</p>
          </div>
          <div className="auth-quote">
            <div className="auth-quote-top">
              <span className="auth-quote-avatar"><BrandMark size={34} /></span>
              <span>Lumio assistant</span>
              <span className="auth-online" aria-hidden />
            </div>
            <p>“Good to see you. What would you like to work on today?”</p>
          </div>
          <p className="auth-foot">A little more clarity, every day.</p>
          <div className="auth-orbit" aria-hidden />
        </section>
        <section className="auth-action">
          <div className="auth-form">
            <div className="auth-eyebrow">Welcome back</div>
            <h2>Sign in to Lumio</h2>
            <p className="auth-intro">Your personal assistant is just a sign-in away.</p>
            {deleted ? <p className="att-summary">Your account and all its data were deleted.</p> : null}
            <AuthProviders error={error} />
            <p className="auth-alt">
              Prefer the browser first? <Link href="/chat">Open the web app</Link>
              {" · "}
              <Link href="/download">Download for Mac or Windows</Link>
            </p>
            <details className="trouble">
              <summary>Having trouble?</summary>
              <p>
                Google: if the Cloud app is in Testing, add your account under Auth Platform → Audience → Test users.
                Outlook: Azure Application (client) ID must be a full GUID. Supported account types must include personal
                Microsoft accounts.
              </p>
            </details>
            <p className="trust">By signing in, you agree this is a private workspace for your accounts.</p>
          </div>
        </section>
      </main>
    </div>
  );
}
