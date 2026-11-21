"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { AuthShell, postJSON, tokenFromQuery } from "../components/AuthShell";

/** Opened from the confirmation email. Confirms with a POST (a mail scanner opening the link doesn't sign anyone in). */
export default function VerifyEmail() {
  const [error, setError] = useState("");
  const started = useRef(false);

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    const token = tokenFromQuery();
    if (!token) {
      setError("This link is missing its code. Open the link from the email again.");
      return;
    }
    postJSON("/api/auth/email/verify", { token }).then((r) => {
      if (r.ok) window.location.replace(r.data.redirect || "/chat");
      else setError(r.error);
    });
  }, []);

  return (
    <AuthShell eyebrow="Confirm email" title={error ? "We couldn't confirm your email" : "Confirming your email…"}>
      {error ? (
        <>
          <p className="err" role="alert">{error}</p>
          <p className="auth-alt">
            <Link href="/signup">Sign up again</Link> · <Link href="/login">Sign in</Link>
          </p>
        </>
      ) : (
        <p className="auth-intro">One moment. You'll be signed in as soon as it's done.</p>
      )}
    </AuthShell>
  );
}
