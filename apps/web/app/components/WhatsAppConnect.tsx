"use client";

import { useEffect, useState } from "react";
import { whatsappLinkCode, whatsappUnlink, type WhatsAppCode, type WhatsAppStatus } from "../lib";

function minutesLeft(iso: string, now: number) {
  const ms = new Date(iso).getTime() - now;
  return ms > 0 ? Math.ceil(ms / 60000) : 0;
}

export function WhatsAppConnect({ status, onChange }: { status: WhatsAppStatus | null; onChange: () => void }) {
  const [code, setCode] = useState<WhatsAppCode | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [now, setNow] = useState(() => Date.now());
  const links = status?.links || [];

  useEffect(() => {
    if (!code) return;
    const tick = window.setInterval(() => {
      setNow(Date.now());
      onChange(); // picks up the link as soon as the code is sent from the phone
    }, 5000);
    return () => window.clearInterval(tick);
  }, [code, onChange]);

  useEffect(() => {
    if (code && links.length) setCode(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [links.length]);

  async function connect() {
    setBusy("code");
    setError("");
    try {
      setCode(await whatsappLinkCode());
      setNow(Date.now());
    } catch (err: any) {
      setError(err.message || "Could not create a link code");
    } finally {
      setBusy("");
    }
  }

  async function unlink(id: string) {
    setBusy(id);
    setError("");
    try {
      await whatsappUnlink(id);
      onChange();
    } catch (err: any) {
      setError(err.message || "Could not disconnect");
    } finally {
      setBusy("");
    }
  }

  const left = code ? minutesLeft(code.expires_at, now) : 0;
  return (
    <div className="card stack">
      <header className="row" style={{ justifyContent: "space-between" }}>
        <strong>WhatsApp</strong>
        <span className={`badge ${links.length ? "ok" : "down"}`}>
          {!status ? "checking" : !status.enabled ? "not set up" : links.length ? "connected" : "not connected"}
        </span>
      </header>
      <p className="muted" style={{ margin: 0 }}>
        Message your agents from WhatsApp. Type normally for your default agent, <code>tony: …</code> to ask a specific
        agent, <code>/use tony</code> to stick with one, <code>/agents</code> to list them. Approvals arrive as buttons.
      </p>
      {status && !status.enabled ? (
        <p className="muted" style={{ margin: 0 }}>
          WhatsApp needs the WHATSAPP_* settings on this server (docs/WHATSAPP.md).
        </p>
      ) : null}
      {links.map((link) => (
        <div key={link.id} className="row" style={{ justifyContent: "space-between", gap: 12 }}>
          <div>
            <strong>{link.address}</strong>
            {link.display_name ? <span className="muted"> · {link.display_name}</span> : null}
            <div className="muted" style={{ fontSize: 13 }}>
              {link.sticky_agent ? `Talking to ${link.sticky_agent}. ` : "Default agent. "}
              {link.window_open ? "Replies go out right away." : "Send any message on WhatsApp to receive replies."}
              {link.queued ? ` ${link.queued} waiting.` : ""}
            </div>
          </div>
          <button className="btn secondary" disabled={busy === link.id} onClick={() => unlink(link.id)}>
            {busy === link.id ? "Disconnecting…" : "Disconnect"}
          </button>
        </div>
      ))}
      {code && left > 0 ? (
        <div className="stack" style={{ gap: 8 }}>
          <p style={{ margin: 0 }}>
            Send this from your phone to {code.business_number || "the Checkme WhatsApp number"}:
          </p>
          <code style={{ fontSize: 22, letterSpacing: 2 }}>{code.message}</code>
          <span className="muted" style={{ fontSize: 13 }}>
            One-time code, expires in {left} min.
          </span>
          {code.wa_link ? (
            <div className="conn-actions">
              <a className="btn" href={code.wa_link} target="_blank" rel="noreferrer">
                Open WhatsApp
              </a>
            </div>
          ) : null}
        </div>
      ) : null}
      {error ? <p className="err" style={{ margin: 0 }}>{error}</p> : null}
      {status?.enabled && !(code && left > 0) ? (
        <div className="conn-actions">
          <button className="btn" disabled={busy === "code"} onClick={connect}>
            {busy === "code" ? "Creating code…" : links.length ? "Connect another number" : "Connect WhatsApp"}
          </button>
        </div>
      ) : null}
    </div>
  );
}
