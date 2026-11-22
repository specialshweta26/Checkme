"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { AuthShell, tokenFromQuery } from "../../components/AuthShell";
import { apiError, getMe } from "../../lib";

// Accept a team invitation (apps/api/app/teams/routes.py "join"). Confirms by POST, so a link scanner joins no one.

export default function JoinTeam() {
  const [state, setState] = useState<"loading" | "signin" | "ready" | "joined" | "error">("loading");
  const [email, setEmail] = useState("");
  const [team, setTeam] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    getMe().then((me) => {
      if (!me) {
        setState("signin");
        return;
      }
      setEmail(me.email);
      setState("ready");
    });
  }, []);

  async function accept() {
    const res = await fetch("/api/teams/join", {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: tokenFromQuery() }),
    });
    const data = await res.json().catch(() => ({}));
    if (res.ok) {
      setTeam(data.name);
      setState("joined");
    } else {
      setError(apiError(data, "This invitation couldn't be accepted."));
      setState("error");
    }
  }


  return (
    <AuthShell eyebrow="Team invitation" title={state === "joined" ? `You joined ${team}` : "Join a team on Lumio"}>
      {state === "signin" ? (
        <p className="auth-intro">
          <Link href="/login">Sign in</Link> or <Link href="/signup">create an account</Link> with the address the invitation was sent to, then
          open this link again.
        </p>
      ) : null}
      {state === "ready" ? (
        <>
          <p className="auth-intro">You&apos;re signed in as {email}. Accept to see the skills your team shares.</p>
          <button className="btn" onClick={accept}>
            Accept invitation
          </button>
        </>
      ) : null}
      {state === "joined" ? (
        <p className="auth-intro">
          <Link href="/teams">Open your teams</Link>
        </p>
      ) : null}
      {state === "error" ? <p className="err">{error}</p> : null}
    </AuthShell>
  );
}
