"use client";

import { useEffect, useState } from "react";
import { authStatus, type AuthStatus } from "./AuthShell";
import type { Me } from "../lib";

// First-run help (docs/AUTH.md "Getting started"). People who signed up with email or SSO have no mailbox yet, so the
// chat would otherwise open on an assistant that can't see their mail or calendar. Also sets the account's time zone
// from the browser once, since every new account starts on the server default.

const DISMISS = "lumio-get-started-hidden";
const TZ_DONE = "lumio-tz-auto";

function store(key: string, value?: string): string | null {
  try {
    if (value !== undefined) window.localStorage.setItem(key, value);
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

export function hasMailbox(me: Me | null) {
  return Boolean(me?.accounts?.some((a) => a.provider === "google" || a.provider === "microsoft"));
}

export function GetStarted({ me, onPick }: { me: Me | null; onPick: (prompt: string) => void }) {
  const [status, setStatus] = useState<AuthStatus | null>(null);
  const [hidden, setHidden] = useState(true);
  const [tzNote, setTzNote] = useState("");

  useEffect(() => {
    setHidden(store(DISMISS) === "1");
    authStatus().then(setStatus);
  }, []);

  useEffect(() => {
    if (!me || store(TZ_DONE)) return;
    let tz = "";
    try {
      tz = Intl.DateTimeFormat().resolvedOptions().timeZone || "";
    } catch {
      tz = "";
    }
    store(TZ_DONE, "1");
    if (!tz || tz === me.timezone) return;
    fetch("/api/me", { method: "PATCH", credentials: "include", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ timezone: tz }) })
      .then((r) => (r.ok ? setTzNote(`Times are now shown in ${tz.replace(/_/g, " ")}, your browser's time zone.`) : undefined))
      .catch(() => undefined);
  }, [me]);

  if (!me || hasMailbox(me) || hidden) return tzNote ? <p className="notice">{tzNote}</p> : null;

  function dismiss() {
    store(DISMISS, "1");
    setHidden(true);
  }

  return (
    <div className="get-started task-card stack">
      <div className="row spread">
        <strong>Get started with Lumio</strong>
        <button className="auth-link link-button" type="button" onClick={dismiss}>
          Hide
        </button>
      </div>
      <span className="muted">
        Lumio works best with your mail and calendar. Connect one, then ask for a morning brief, a reply draft or a meeting slot.
      </span>
      {tzNote ? <span className="faint">{tzNote}</span> : null}
      <div className="row wrap">
        {status?.google !== false ? (
          <a className="btn" href="/api/auth/google/login?mode=link">
            Connect Gmail
          </a>
        ) : null}
        {status?.microsoft !== false ? (
          <a className="btn" href="/api/auth/microsoft/login?mode=link">
            Connect Outlook
          </a>
        ) : null}
        <a className="btn secondary" href="/connectors">
          Add other apps
        </a>
        <button className="btn secondary" type="button" onClick={() => onPick("Here's a bit about me and my work, so you can help me better: ")}>
          Tell Lumio about you
        </button>
      </div>
    </div>
  );
}
