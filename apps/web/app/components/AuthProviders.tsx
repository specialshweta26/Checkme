"use client";

import { useEffect, useRef, useState, type JSX } from "react";
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

type Provider = "google" | "microsoft";

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
export function AuthProviders({ error = "" }: { error?: string }) {
  const [waiting, setWaiting] = useState<Provider | null>(null);
  const [problem, setProblem] = useState("");
  const stopRef = useRef(false);
  // Known only in the browser (the server renders the web version first).
  const [desktop, setDesktop] = useState(false);

  useEffect(() => {
    setDesktop(isLumioDesktop());
    return () => {
      stopRef.current = true;
    };
  }, []);

  async function desktopSignIn(provider: Provider) {
    const tauri = (window as { __TAURI__?: { core?: { invoke?: (name: string, args: { url: string }) => Promise<unknown> } } }).__TAURI__?.core;
    if (!tauri?.invoke) {
      window.location.href = `/api/auth/${provider}/login`;
      return;
    }
    const code = newCode();
    setProblem("");
    setWaiting(provider);
    stopRef.current = false;
    try {
      await tauri.invoke("open_external", { url: `${window.location.origin}/api/auth/${provider}/login?desktop=${code}` });
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
      </div>
    </>
  );
}
