"use client";

import { useEffect, useMemo, useState, type ReactElement } from "react";
import { AppShell } from "../components/AppShell";
import { IconArtifact, IconCopy, IconFile, IconLink, IconText } from "../components/ProfileIcons";
import { chatHref, getMe, relativeTime, type Me } from "../lib";

// Artifacts (GET /api/skills/artifacts, apps/api/app/skills/task_state.py artifact_feed): what the user's agents
// produced while following skills (links, files, IDs, quotes), newest first, with the chat or task it came from.

type Artifact = {
  at: string;
  label: string;
  value: string;
  by: string;
  skill_name: string;
  status: string;
  session_id: string | null;
  job_id: string | null;
  agent_id: string | null;
};
type Kind = "link" | "file" | "text";
type Filter = "all" | Kind;

const FILE = /\.(pdf|docx?|xlsx?|csv|pptx?|png|jpe?g|gif|webp|zip|txt|md|json|html?)(\?.*)?$/i;

function kindOf(a: Artifact): Kind {
  const v = a.value.trim();
  if (FILE.test(v) || /^\/(?!\/)\S+$/.test(v)) return "file";
  if (/^https?:\/\/\S+$/i.test(v)) return "link";
  return "text";
}

function hostOf(url: string) {
  try {
    return new URL(url).host.replace(/^www\./, "");
  } catch {
    return "";
  }
}

const ICON: Record<Kind, (p: { size?: number }) => ReactElement> = { link: IconLink, file: IconFile, text: IconText };
const NAME: Record<Filter, string> = { all: "All", link: "Links", file: "Files", text: "Notes and IDs" };

export default function ArtifactsPage() {
  const [me, setMe] = useState<Me | null>(null);
  const [items, setItems] = useState<Artifact[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState("");
  const [filter, setFilter] = useState<Filter>("all");
  const [query, setQuery] = useState("");
  const [copied, setCopied] = useState("");

  useEffect(() => {
    getMe().then((u) => {
      if (!u) {
        window.location.replace("/login");
        return;
      }
      setMe(u);
      fetch("/api/skills/artifacts", { credentials: "include" })
        .then((r) => (r.ok ? r.json() : Promise.reject(new Error("Couldn't load your artifacts."))))
        .then((d) => setItems(d.artifacts || []))
        .catch((e) => setError(e.message))
        .finally(() => setLoaded(true));
    });
  }, []);

  const counts = useMemo(() => {
    const out: Record<Filter, number> = { all: items.length, link: 0, file: 0, text: 0 };
    items.forEach((a) => (out[kindOf(a)] += 1));
    return out;
  }, [items]);

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    return items.filter((a) => (filter === "all" || kindOf(a) === filter) && (!q || `${a.label} ${a.value} ${a.skill_name} ${a.by}`.toLowerCase().includes(q)));
  }, [items, filter, query]);

  async function copy(a: Artifact) {
    try {
      await navigator.clipboard.writeText(a.value);
      setCopied(a.at + a.value);
      window.setTimeout(() => setCopied(""), 1600);
    } catch {
      setError("Couldn't copy. Select the text and copy it instead.");
    }
  }

  return (
    <AppShell me={me} current="artifacts">
      <div className="page">
        <div className="tasks-page">
          <div className="page-hero">
            <div>
              <span className="eyebrow">Profile</span>
              <h1>Artifacts</h1>
              <p className="muted">
                Links, files, IDs and notes your agents produced while working through a skill. Copy what you need, or open the chat it
                came from to see how it was made.
              </p>
            </div>
          </div>

          {items.length ? (
            <div className="tasks-toolbar">
              <div className="tasks-tabs" role="tablist" aria-label="Kind">
                {(Object.keys(NAME) as Filter[]).map((id) => (
                  <button key={id} role="tab" aria-selected={filter === id} className={`pill${filter === id ? " on" : ""}`} onClick={() => setFilter(id)}>
                    {NAME[id]} <span className="pill-count">{counts[id]}</span>
                  </button>
                ))}
              </div>
              <div className="tasks-filters">
                <input className="tasks-search" type="search" aria-label="Search artifacts" placeholder="Search artifacts" value={query} onChange={(e) => setQuery(e.target.value)} />
              </div>
            </div>
          ) : null}

          {error ? <p className="err">{error}</p> : null}

          {loaded && items.length === 0 && !error ? (
            <div className="task-empty showcase-empty">
              <span className="showcase-icon tone-violet">
                <IconArtifact size={26} />
              </span>
              <strong>Nothing made yet</strong>
              <p className="muted">
                When an agent follows a skill and produces something worth keeping (a report link, a file, an order number), it lands
                here.
              </p>
              <a className="btn" href="/chat">
                Talk to Chief
              </a>
            </div>
          ) : null}

          {loaded && items.length > 0 && shown.length === 0 ? <p className="muted">Nothing matches.</p> : null}

          <div className="artifact-grid">
            {shown.map((a) => {
              const kind = kindOf(a);
              const Icon = ICON[kind];
              const url = /^https?:\/\//i.test(a.value) ? a.value : "";
              const from = a.session_id ? chatHref({ agent: a.agent_id, conversation: a.session_id }) : a.job_id ? "/routines?tab=history" : "";
              return (
                <article key={a.at + a.value} className="artifact-card">
                  <header>
                    <span className={`artifact-kind kind-${kind}`}>
                      <Icon size={16} />
                    </span>
                    <div className="artifact-title">
                      <strong>{a.label || NAME[kind].replace(/s$/, "")}</strong>
                      <span className="faint">{relativeTime(a.at)}</span>
                    </div>
                  </header>
                  {url ? (
                    <a className="artifact-value link" href={url} target="_blank" rel="noopener noreferrer">
                      <span className="artifact-host">{hostOf(url)}</span>
                      <span className="artifact-url">{url}</span>
                    </a>
                  ) : (
                    <p className={`artifact-value${a.value.length < 40 && !/\s/.test(a.value) ? " mono" : ""}`}>{a.value}</p>
                  )}
                  <footer>
                    <span className="artifact-source">
                      {a.skill_name ? <span className="app-pill">{a.skill_name}</span> : null}
                      {a.by ? <span className="faint">by {a.by}</span> : null}
                    </span>
                    <span className="artifact-actions">
                      {from ? (
                        <a className="btn secondary small" href={from}>
                          {a.session_id ? "Open chat" : "Open routines"}
                        </a>
                      ) : null}
                      <button className="btn secondary small" type="button" onClick={() => copy(a)} aria-label={`Copy ${a.label || "value"}`}>
                        <IconCopy size={14} /> {copied === a.at + a.value ? "Copied" : "Copy"}
                      </button>
                    </span>
                  </footer>
                </article>
              );
            })}
          </div>
        </div>
      </div>
    </AppShell>
  );
}
