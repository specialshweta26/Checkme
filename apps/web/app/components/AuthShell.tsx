"use client";

import { useEffect, type ReactNode } from "react";
import Link from "next/link";
import { BrandMark } from "./BrandMark";
import { applySavedTheme } from "../lib";

/** The two-panel layout shared by sign-in, sign-up and the password pages. */
export function AuthShell({ eyebrow, title, intro, children }: { eyebrow: string; title: string; intro?: string; children: ReactNode }) {
  useEffect(() => applySavedTheme(), []);
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
            <div className="auth-eyebrow">{eyebrow}</div>
            <h2>{title}</h2>
            {intro ? <p className="auth-intro">{intro}</p> : null}
            {children}
          </div>
        </section>
      </main>
    </div>
  );
}

export type AuthStatus = { google: boolean; microsoft: boolean; email: boolean; sso: boolean; dev_login: boolean; signup?: "open" | "invite" | "closed" };

export async function authStatus(): Promise<AuthStatus> {
  try {
    const res = await fetch("/api/auth/status", { credentials: "include" });
    if (res.ok) return await res.json();
  } catch {
    // fall through
  }
  return { google: true, microsoft: true, email: false, sso: false, dev_login: false };
}

export type ApiData = { redirect?: string; message?: string; detail?: unknown; sso?: boolean; id?: string; name?: string; enforce?: boolean };

/** POST JSON to the API; the error text is the API's ``detail``. */
export async function postJSON(path: string, body: unknown): Promise<{ ok: boolean; status: number; data: ApiData; error: string }> {
  try {
    const res = await fetch(path, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data: ApiData = await res.json().catch(() => ({}));
    const detail = typeof data?.detail === "string" ? data.detail : res.ok ? "" : "Something went wrong. Try again.";
    return { ok: res.ok, status: res.status, data, error: detail };
  } catch {
    return { ok: false, status: 0, data: {}, error: "Can't reach Lumio right now. Check your connection and try again." };
  }
}

export function tokenFromQuery(): string {
  if (typeof window === "undefined") return "";
  return new URLSearchParams(window.location.search).get("token") || "";
}
