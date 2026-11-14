"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { applySavedTheme } from "../lib";

// Shown in the system browser after a sign-in that the Lumio desktop app started (docs/LLD.md "Desktop sign-in").
export default function DesktopSignedIn() {
  const [failed, setFailed] = useState(false);
  useEffect(() => applySavedTheme(), []);
  useEffect(() => {
    setFailed(new URLSearchParams(window.location.search).get("error") === "handoff");
  }, []);
  return (
    <div className="auth">
      <main className="auth-shell" style={{ gridTemplateColumns: "1fr" }}>
        <section className="auth-action">
          <div className="auth-form">
            <div className="auth-eyebrow">Lumio desktop</div>
            {failed ? (
              <>
                <h2>Almost there</h2>
                <p className="auth-intro">You're signed in here, but the desktop app couldn't be signed in. Go back to the app and try again.</p>
              </>
            ) : (
              <>
                <h2>You're signed in</h2>
                <p className="auth-intro">Go back to the Lumio app: it has signed in too. You can close this tab.</p>
              </>
            )}
            <p className="auth-alt">
              Or <Link href="/chat">open Lumio in this browser</Link>.
            </p>
          </div>
        </section>
      </main>
    </div>
  );
}
