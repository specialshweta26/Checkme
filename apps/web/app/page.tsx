"use client";

import { useEffect, useState } from "react";
import { BrandMark } from "./components/BrandMark";
import { errorFromQuery, getMe } from "./lib";

export default function Home() {
  const [error, setError] = useState("");

  useEffect(() => {
    setError(errorFromQuery(window.location.search));
    getMe().then((me) => {
      if (me) window.location.replace("/chat");
    });
  }, []);

  return (
    <div className="auth">
      <main className="auth-shell">
        <section className="auth-visual" aria-label="Lumio">
          <div className="auth-brand">
            <span className="auth-mark">
              <BrandMark size={22} />
            </span>
            lumio
          </div>
          <div className="auth-hero-copy">
            <h1>Your personal assistant, ready when you are.</h1>
            <p>Bring your day into focus. Lumio helps you think, plan, and get things done—one conversation at a time.</p>
          </div>
          <div className="auth-quote">
            <div className="auth-quote-top">
              <span className="auth-quote-avatar">✦</span>
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
            {error ? <p className="err">{error}</p> : null}
            <div className="providers">
              <a className="provider" href="/api/auth/google/login">
                <GoogleMark />
                Continue with Google
              </a>
              <a className="provider" href="/api/auth/microsoft/login">
                <MicrosoftMark />
                Continue with Microsoft
              </a>
            </div>
            <details className="trouble">
              <summary>Having trouble?</summary>
              <p>
                Google: if the Cloud app is in Testing, add your account under Auth Platform → Audience → Test users.
                Outlook: Azure Application (client) ID must be a full GUID. Supported account types must include personal
                Microsoft accounts. Redirect URI: <code>http://localhost:3000/api/auth/microsoft/callback</code>. Use the
                client secret Value, not the Secret ID.
              </p>
            </details>
            <p className="trust">By signing in, you agree this is a private workspace for your accounts.</p>
          </div>
        </section>
      </main>
    </div>
  );
}

function GoogleMark() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" aria-hidden>
      <path fill="#4285F4" d="M23.5 12.3c0-.8-.1-1.6-.2-2.3H12v4.4h6.5c-.3 1.5-1.1 2.8-2.4 3.7v3h3.9c2.3-2.1 3.5-5.2 3.5-8.8z" />
      <path fill="#34A853" d="M12 24c3.2 0 5.9-1 7.9-2.9l-3.9-3c-1.1.7-2.4 1.2-4 1.2-3.1 0-5.7-2.1-6.6-4.9H1.4v3.1C3.4 21.4 7.4 24 12 24z" />
      <path fill="#FBBC05" d="M5.4 14.4c-.2-.7-.4-1.4-.4-2.4s.1-1.7.4-2.4V6.5H1.4C.5 8.3 0 10.1 0 12s.5 3.7 1.4 5.5l4-3.1z" />
      <path fill="#EA4335" d="M12 4.8c1.7 0 3.3.6 4.5 1.8l3.4-3.4C17.9 1.1 15.2 0 12 0 7.4 0 3.4 2.6 1.4 6.5l4 3.1C6.3 6.8 8.9 4.8 12 4.8z" />
    </svg>
  );
}

function MicrosoftMark() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" aria-hidden>
      <path fill="#F25022" d="M1 1h10v10H1z" />
      <path fill="#7FBA00" d="M13 1h10v10H13z" />
      <path fill="#00A4EF" d="M1 13h10v10H1z" />
      <path fill="#FFB900" d="M13 13h10v10H13z" />
    </svg>
  );
}
