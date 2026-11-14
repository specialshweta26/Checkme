"use client";

import { useEffect, useState, type ReactNode } from "react";
import { AppShell } from "../components/AppShell";
import { IconArrow, IconArtifact, IconData, IconGauge, IconKnow, IconPlugSm, IconRoutines, IconRuns, IconSkills, IconVault } from "../components/ProfileIcons";
import { getMe, initials, type Me } from "../lib";

// Profile (docs/DESIGN.md "Profile"): who is signed in, and one place for everything the workspace keeps for them:
// What I know, Artifacts, Saved runs and the Vault, then connectors, usage and their data.

type Counts = { people?: number; facts?: number; artifacts?: number; runs?: number; logins?: number; skills?: number; routines?: number; failed?: number };

async function count(path: string, pick: (data: any) => number | undefined): Promise<number | undefined> {
  try {
    const res = await fetch(path, { credentials: "include" });
    return res.ok ? pick(await res.json()) : undefined;
  } catch {
    return undefined;
  }
}

const PROVIDER: Record<string, string> = { google: "Google", microsoft: "Microsoft", dev: "Local demo" };

function Card({ href, icon, tone, title, body, meta }: { href: string; icon: ReactNode; tone: string; title: string; body: string; meta?: string }) {
  return (
    <a className={`profile-card tone-${tone}`} href={href}>
      <span className="profile-card-icon">{icon}</span>
      <span className="profile-card-copy">
        <strong>{title}</strong>
        <span>{body}</span>
      </span>
      <span className="profile-card-foot">
        {meta ? <span className="profile-card-meta">{meta}</span> : <span />}
        <span className="profile-card-go" aria-hidden>
          <IconArrow size={16} />
        </span>
      </span>
    </a>
  );
}

const show = (n: number | undefined, one: string, many: string) => (n === undefined ? "" : `${n.toLocaleString()} ${n === 1 ? one : many}`);

export default function ProfilePage() {
  const [me, setMe] = useState<Me | null>(null);
  const [c, setC] = useState<Counts>({});

  useEffect(() => {
    getMe().then((u) => {
      if (!u) {
        window.location.replace("/login");
        return;
      }
      setMe(u);
      Promise.all([
        count("/api/memory/people", (d) => d?.people?.length),
        count("/api/memory/facts", (d) => d?.facts?.length),
        count("/api/skills/artifacts", (d) => d?.artifacts?.length),
        count("/api/browser/saved-runs", (d) => (Array.isArray(d) ? d.length : undefined)),
        count("/api/browser/logins", (d) => (Array.isArray(d) ? d.length : undefined)),
        count("/api/skills", (d) => d?.skills?.length),
        count("/api/tasks/summary?days=30", (d) => d?.routines),
        count("/api/tasks/summary?days=30", (d) => d?.failed),
      ]).then(([people, facts, artifacts, runs, logins, skills, routines, failed]) => setC({ people, facts, artifacts, runs, logins, skills, routines, failed }));
    });
  }, []);

  const connected = me?.connectors.filter((x) => x.enabled).length;
  const provider = me ? PROVIDER[me.primary_provider] || me.primary_provider : "";
  const stats: [string, number | undefined][] = [
    ["People known", c.people],
    ["Facts", c.facts],
    ["Artifacts", c.artifacts],
    ["Saved runs", c.runs],
    ["In your vault", c.logins],
  ];

  return (
    <AppShell me={me} current="profile">
      <div className="page profile-page">
        <section className="profile-hero">
          <span className="profile-avatar" aria-hidden>
            {me ? initials(me.name, me.email) : ""}
          </span>
          <div className="profile-id">
            <span className="eyebrow">Profile</span>
            <h1>{me?.name || me?.email || "Loading…"}</h1>
            <p>
              {me?.email}
              {provider ? <span className="profile-provider">Signed in with {provider}</span> : null}
            </p>
            {me?.accounts.length ? (
              <div className="profile-accounts">
                {me.accounts.map((a) => (
                  <span key={`${a.provider}${a.email}`} className="profile-chip">
                    {PROVIDER[a.provider] || a.provider} · {a.email}
                  </span>
                ))}
              </div>
            ) : null}
          </div>
          <div className="profile-hero-actions">
            <a className="btn secondary" href="/connectors">
              Manage connectors
            </a>
          </div>
        </section>

        <section className="profile-stats" aria-label="Your workspace at a glance">
          {stats.map(([label, n]) => (
            <div key={label} className="profile-stat">
              <strong>{n === undefined ? "–" : n.toLocaleString()}</strong>
              <span>{label}</span>
            </div>
          ))}
        </section>

        <h2 className="profile-section">Your workspace</h2>
        <div className="profile-grid">
          <Card
            href="/memory"
            tone="accent"
            icon={<IconKnow size={20} />}
            title="What I know"
            body="People, facts and what changed this week. Your agents use it to draft replies and prepare meetings."
            meta={[show(c.people, "person", "people"), show(c.facts, "fact", "facts")].filter(Boolean).join(" · ")}
          />
          <Card
            href="/routines"
            tone="ok"
            icon={<IconRoutines size={20} />}
            title="Routines"
            body="Work your agents do on a schedule: each agent's routines, how every run went, and what failed and why."
            meta={[show(c.routines, "routine", "routines"), c.failed ? `${c.failed} failed this month` : ""].filter(Boolean).join(" · ")}
          />
          <Card
            href="/skills"
            tone="accent"
            icon={<IconSkills size={20} />}
            title="Skills"
            body="Ready-made skills in Claude's SKILL.md format, from Anthropic, Trinity or any GitHub library. Export yours the same way."
            meta={show(c.skills, "skill", "skills")}
          />
          <Card
            href="/artifacts"
            tone="violet"
            icon={<IconArtifact size={20} />}
            title="Artifacts"
            body="Links, files, IDs and notes your agents produced while they worked. Copy them or open the chat they came from."
            meta={show(c.artifacts, "item", "items")}
          />
          <Card
            href="/runs"
            tone="ok"
            icon={<IconRuns size={20} />}
            title="Saved runs"
            body="Browser work your agents learned, replayed step by step. Run it again, on a schedule or once per row."
            meta={show(c.runs, "run", "runs")}
          />
          <Card
            href="/vault"
            tone="vault"
            icon={<IconVault size={20} />}
            title="Vault"
            body="Encrypted sign-ins for sites your agents work on. They type them in; they never see them."
            meta={show(c.logins, "sign-in", "sign-ins")}
          />
        </div>

        <h2 className="profile-section">Account</h2>
        <div className="profile-grid compact">
          <Card
            href="/connectors"
            tone="neutral"
            icon={<IconPlugSm size={18} />}
            title="Connectors"
            body="Mail, calendar and the apps your agents can use."
            meta={connected === undefined ? "" : `${connected} connected`}
          />
          <Card href="/usage" tone="neutral" icon={<IconGauge size={18} />} title="Usage and cost" body="Model use, searches and computer time for the last 7 days." />
          <Card href="/account" tone="neutral" icon={<IconData size={18} />} title="Your data" body="Download everything, or delete your account." />
        </div>
      </div>
    </AppShell>
  );
}
