"use client";

import { useEffect, useRef, useState, type FormEvent, type JSX } from "react";
import { isLumioDesktop } from "../lib";
export function GoogleMark() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" aria-hidden>
      <path fill="#4285F4" d="M23.5 12.3c0-.8-.1-1.6-.2-2.3H12v4.4h6.5c-.3 1.5-1.1 2.8-2.4 3.7v3h3.9c2.3-2.1 3.5-5.2 3.5-8.8z" />
      <path fill="#34A853" d="M12 24c3.2 0 5.9-1 7.9-2.9l-3.9-3c-1.1.7-2.4 1.2-4 1.2-3.1 0-5.7-2.1-6.6-4.9H1.4v3.1C3.4 21.4 7.4 24 12 24z" />
      <path fill="#FBBC05" d="M5.4 14.4c-.2-.7-.4-1.4-.4-2.4s.1-1.7.4-2.4V6.5H1.4C.5 8.3 0 10.1 0 12s.5 3.7 1.4 5.5l4-3.1z" />
      <path fill="#EA4335" d="M12 4.8c1.7 0 3.3.6 4.5 1.8l3.4-3.4C17.9 1.1 15.2 0 12 0 7.4 0 3.4 2.6 1.4 6.5l4 3.1C6.3 6.8 8.9 4.8 12 4.8z" />
    </svg>
  );
}

export function MicrosoftMark() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" aria-hidden>
      <path fill="#F25022" d="M1 1h10v10H1z" />
      <path fill="#7FBA00" d="M13 1h10v10H13z" />
      <path fill="#00A4EF" d="M1 13h10v10H1z" />
      <path fill="#FFB900" d="M13 13h10v10H13z" />
    </svg>
  );
}

type Provider = string;

function KeyMark() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <circle cx="7.5" cy="15.5" r="4.5" />
      <path d="M10.7 12.3 21 2M16 7l3 3M18.5 4.5l2 2" />
    </svg>
  );
}

function newCode() {
  const bytes = new Uint8Array(32);
  crypto.getRandomValues(bytes);
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/**
 * Sign-in buttons. In the Lumio desktop app the provider's page opens in the system browser (it can't sign in
 * inside the app window), so the app starts sign-in there with a one-time code and waits until the browser has
 * finished, then collects its own session (docs/LLD.md "Desktop sign-in").
 */
export function AuthProviders({ error = "", sso = false }: { error?: string; sso?: boolean }) {
  const [waiting, setWaiting] = useState<Provider | null>(null);
  const [problem, setProblem] = useState("");
  const [ssoOpen, setSsoOpen] = useState(false);
  const [workEmail, setWorkEmail] = useState("");
  const [ssoBusy, setSsoBusy] = useState(false);
  const stopRef = useRef(false);
  // Known only in the browser (the server renders the web version first).
  const [desktop, setDesktop] = useState(false);

  useEffect(() => {
    setDesktop(isLumioDesktop());
    return () => {
      stopRef.current = true;
    };
  }, []);

  async function desktopSignIn(provider: Provider, path = `/api/auth/${provider}/login`) {
    const tauri = (window as { __TAURI__?: { core?: { invoke?: (name: string, args: { url: string }) => Promise<unknown> } } }).__TAURI__?.core;
    if (!tauri?.invoke) {
      window.location.href = path;
      return;
    }
    const code = newCode();
    setProblem("");
    setWaiting(provider);
    stopRef.current = false;
    try {
      const sep = path.includes("?") ? "&" : "?";
      await tauri.invoke("open_external", { url: `${window.location.origin}${path}${sep}desktop=${code}` });
    } catch {
      setWaiting(null);
      setProblem("Couldn't open your browser to sign in.");
      return;
    }
    const until = Date.now() + 10 * 60 * 1000;
    while (!stopRef.current && Date.now() < until) {
      await new Promise((r) => window.setTimeout(r, 2000));
      if (stopRef.current) return;
      try {
        const res = await fetch("/api/auth/desktop/claim", {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ code }),
        });
        const data = await res.json().catch(() => ({}));
        if (res.ok && data.ok) {
          window.location.replace("/chat");
          return;
        }
      } catch {
        // Offline for a moment: keep waiting.
      }
    }
    if (!stopRef.current) {
      setWaiting(null);
      setProblem("Sign-in timed out. Try again.");
    }
  }

  async function startSso(e: FormEvent) {
    e.preventDefault();
    const email = workEmail.trim();
    if (!email.includes("@")) {
      setProblem("Enter your work email.");
      return;
    }
    setSsoBusy(true);
    setProblem("");
    try {
      const res = await fetch(`/api/auth/sso/discover?email=${encodeURIComponent(email)}`, { credentials: "include" });
      const data = await res.json().catch(() => ({}));
      if (!data?.sso || !data?.id) {
        setProblem("Single sign-on isn't set up for that email's company. Use another way to sign in, or ask your admin.");
        return;
      }
      const path = `/api/auth/sso/${encodeURIComponent(data.id)}/login?email=${encodeURIComponent(email)}`;
      if (desktop) await desktopSignIn(`sso:${data.id}`, path);
      else window.location.href = path;
    } catch {
      setProblem("Can't reach Lumio right now. Try again.");
    } finally {
      setSsoBusy(false);
    }
  }

  const button = (provider: Provider, label: string, mark: JSX.Element) =>
    desktop ? (
      <button className="provider" type="button" disabled={waiting !== null} onClick={() => void desktopSignIn(provider)}>
        {mark}
        {label}
      </button>
    ) : (
      <a className="provider" href={`/api/auth/${provider}/login`}>
        {mark}
        {label}
      </a>
    );

  return (
    <>
      {error || problem ? <p className="err">{problem || error}</p> : null}
      {waiting ? (
        <div className="desktop-wait">
          <strong>Finish signing in in your browser</strong>
          <span className="muted">This window signs you in by itself as soon as you're done there.</span>
          <button
            className="btn secondary small"
            type="button"
            onClick={() => {
              stopRef.current = true;
              setWaiting(null);
            }}
          >
            Cancel
          </button>
        </div>
      ) : null}
      <div className="providers">
        {button("google", "Continue with Google", <GoogleMark />)}
        {button("microsoft", "Continue with Microsoft", <MicrosoftMark />)}
        {sso && !ssoOpen ? (
          <button className="provider" type="button" disabled={waiting !== null} onClick={() => setSsoOpen(true)}>
            <KeyMark />
            Continue with SSO
          </button>
        ) : null}
      </div>
      {sso && ssoOpen ? (
        <form className="auth-fields sso-form" onSubmit={startSso}>
          <label>
            <span>Work email</span>
            <input type="email" autoComplete="email" required autoFocus value={workEmail} onChange={(e) => setWorkEmail(e.target.value)} placeholder="you@company.com" />
          </label>
          <button className="btn large block" type="submit" disabled={ssoBusy || waiting !== null}>
            {ssoBusy ? "Finding your company…" : "Continue with SSO"}
          </button>
        </form>
      ) : null}
    </>
  );
}
