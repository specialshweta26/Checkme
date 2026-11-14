"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { AppShell } from "../components/AppShell";
import { SkillDetail } from "../components/SkillDetail";
import { IconLock, IconPlus, IconSkills } from "../components/ProfileIcons";
import {
  getMe,
  importedFrom,
  importLibrarySkills,
  listSkills,
  loadWorkspace,
  relativeTime,
  skillExportUrl,
  skillLibrary,
  skillLibrarySources,
  skillUpdateFromSource,
  uploadSkill,
  type AgentSkill,
  type ImportResult,
  type LibraryCatalog,
  type LibrarySource,
  type Me,
  type WorkspaceAgent,
} from "../lib";
import { AgentSelect } from "../components/Select";

// Skills library (docs/SKILLS.md): add Claude-format skills (a folder with SKILL.md) from a GitHub library or a file,
// and export any skill as a .zip that Claude, Claude Code or another Lumio can use.

type Tab = "browse" | "upload" | "mine";

function kindLabel(skill: AgentSkill) {
  return skill.kind === "imported" ? "Imported" : skill.kind === "taught" ? "Taught" : "Saved";
}

function summary(result: ImportResult) {
  const added = result.imported.filter((r) => r.created).length;
  const updated = result.imported.length - added;
  const left = result.imported.reduce((n, r) => n + r.skipped.length, 0);
  const bits = [added ? `Added ${added} skill${added === 1 ? "" : "s"}` : "", updated ? `updated ${updated}` : ""].filter(Boolean);
  let text = bits.join(", ") || "Nothing imported";
  text = text[0].toUpperCase() + text.slice(1) + ".";
  if (left) text += ` Left out ${left} file${left === 1 ? "" : "s"} that weren't text (images, fonts) or were too large.`;
  if (result.errors.length) text += ` ${result.errors.length} couldn't be imported: ${result.errors[0].error}`;
  return text;
}

export default function SkillsPage() {
  const [me, setMe] = useState<Me | null>(null);
  const [tab, setTab] = useState<Tab>("browse");
  const [agents, setAgents] = useState<WorkspaceAgent[]>([]);
  const [target, setTarget] = useState("");
  const [skills, setSkills] = useState<AgentSkill[]>([]);
  const [sources, setSources] = useState<LibrarySource[]>([]);
  const [url, setUrl] = useState("");
  const [catalog, setCatalog] = useState<LibraryCatalog | null>(null);
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const [drag, setDrag] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  async function refreshSkills() {
    setSkills((await listSkills()).skills || []);
  }

  useEffect(() => {
    getMe().then((u) => {
      if (!u) {
        window.location.replace("/login");
        return;
      }
      setMe(u);
      loadWorkspace().then((w) => setAgents((w.agents || []).filter((a) => !a.archived)));
      refreshSkills();
      skillLibrarySources()
        .then((d) => {
          setSources(d.sources);
          if (d.sources[0]) browse(d.sources[0].url);
        })
        .catch((e) => setError(e.message));
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function browse(link: string) {
    const next = link.trim();
    if (!next) return;
    setUrl(next);
    setBusy("browse");
    setError("");
    setNotice("");
    setQuery("");
    try {
      setCatalog(await skillLibrary(next));
    } catch (e: any) {
      setCatalog(null);
      setError(e.message);
    } finally {
      setBusy("");
    }
  }

  async function add(path: string) {
    if (!catalog) return;
    setBusy(`add:${path}`);
    setError("");
    try {
      const result = await importLibrarySkills({ url: catalog.source.url, paths: [path], ref: catalog.source.sha, agent_id: target || null });
      setNotice(summary(result));
      await refreshSkills();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy("");
    }
  }

  async function upload(file: File | undefined) {
    if (!file) return;
    setBusy("upload");
    setError("");
    setNotice("");
    try {
      const result = await uploadSkill(file, target || null);
      setNotice(summary(result));
      await refreshSkills();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy("");
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  async function checkUpdate(skill: AgentSkill) {
    setBusy(`upd:${skill.id}`);
    setError("");
    try {
      const check = await skillUpdateFromSource(skill.id, true);
      if (!check.update) {
        setNotice(`“${skill.name}” is up to date with its library.`);
      } else if (window.confirm(`A newer version of “${skill.name}” is in its library. Update it? Edits you made to its instructions here will be replaced.`)) {
        await skillUpdateFromSource(skill.id);
        setNotice(`Updated “${skill.name}” to the library's latest version.`);
        await refreshSkills();
      }
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy("");
    }
  }

  const agentName = (id?: string | null) => (id ? agents.find((a) => a.id === id)?.name || "an agent" : "All agents");
  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    const rows = catalog?.skills || [];
    return q ? rows.filter((s) => `${s.title} ${s.name} ${s.description} ${s.category || ""}`.toLowerCase().includes(q)) : rows;
  }, [catalog, query]);
  const imported = skills.filter((s) => s.kind === "imported").length;

  return (
    <AppShell me={me} current="skills">
      <div className="page">
        <div className="tasks-page">
          <div className="page-hero">
            <div>
              <span className="eyebrow">Profile</span>
              <h1>Skills</h1>
              <p className="muted">
                Add ready-made skills in Claude's SKILL.md format from a library or a file, so your agents can do new kinds of work without
                you teaching each one. Any skill can also be exported for Claude or Claude Code.
              </p>
            </div>
            <div className="skills-target">
              <span>Add skills to</span>
              <AgentSelect value={target} onChange={setTarget} label="Add skills to" allLabel="All agents" agents={agents} />
            </div>
          </div>

          <div className="tasks-toolbar">
            <div className="tasks-tabs" role="tablist" aria-label="Skills">
              {(
                [
                  ["browse", "Browse libraries"],
                  ["upload", "Upload"],
                  ["mine", "Your skills"],
                ] as [Tab, string][]
              ).map(([id, label]) => (
                <button key={id} role="tab" aria-selected={tab === id} className={`pill${tab === id ? " on" : ""}`} onClick={() => setTab(id)}>
                  {label}
                  {id === "mine" ? <span className="pill-count">{skills.length}</span> : null}
                </button>
              ))}
            </div>
            {tab === "browse" && catalog ? (
              <div className="tasks-filters">
                <input className="tasks-search" type="search" aria-label="Search this library" placeholder="Search this library" value={query} onChange={(e) => setQuery(e.target.value)} />
              </div>
            ) : null}
          </div>

          {error ? <p className="err">{error}</p> : null}
          {notice ? <p className="notice">{notice}</p> : null}

          {tab === "browse" ? (
            <>
              <div className="library-sources">
                {sources.map((s) => (
                  <button key={s.id} type="button" className={`library-source${url === s.url ? " on" : ""}`} onClick={() => browse(s.url)}>
                    <strong>{s.name}</strong>
                    <span>{s.about}</span>
                    <code>{s.url.replace("https://", "")}</code>
                  </button>
                ))}
                <form
                  className="library-source library-url"
                  onSubmit={(e) => {
                    e.preventDefault();
                    browse(url);
                  }}
                >
                  <strong>Any GitHub library</strong>
                  <span>A public repository, or one folder of it, with SKILL.md folders inside.</span>
                  <div className="row">
                    <input aria-label="GitHub link" placeholder="github.com/owner/repo" value={url} onChange={(e) => setUrl(e.target.value)} />
                    <button className="btn secondary" disabled={busy === "browse" || !url.trim()}>
                      {busy === "browse" ? "Reading…" : "Browse"}
                    </button>
                  </div>
                </form>
              </div>

              {catalog ? (
                <p className="library-meta muted">
                  <a href={catalog.source.url} target="_blank" rel="noopener noreferrer">
                    {catalog.source.label}
                  </a>{" "}
                  · {catalog.skills.length} skills · {catalog.source.ref} at <code>{catalog.source.sha.slice(0, 7)}</code>
                  {catalog.more ? ` · showing the first ${catalog.skills.length}` : ""}
                </p>
              ) : null}
              {busy === "browse" ? <p className="muted">Reading the library…</p> : null}

              <div className="library-grid">
                {shown.map((s) => {
                  const have = catalog ? importedFrom(skills, catalog.source.url, s.path) : [];
                  const here = have.find((h) => (h.agent_id || "") === target);
                  return (
                    <article key={s.path} className={`library-card${s.error ? " broken" : ""}`}>
                      <header>
                        <span className="library-icon" aria-hidden>
                          {s.title.slice(0, 1).toUpperCase()}
                        </span>
                        <div>
                          <strong>{s.title}</strong>
                          <code>{s.name}</code>
                        </div>
                      </header>
                      <p>{s.error || s.description || "No description."}</p>
                      <div className="library-chips">
                        {s.category ? <span className="app-pill">{s.category.replace(/-/g, " ")}</span> : null}
                        <span className="app-pill">
                          {s.files} file{s.files === 1 ? "" : "s"}
                        </span>
                        {s.has_scripts ? (
                          <span className="app-pill warn" title="Scripts are stored as text and never run on their own">
                            has scripts
                          </span>
                        ) : null}
                        {s.license ? (
                          <span className="app-pill" title={s.license}>
                            {s.license.length > 26 ? "licence: see repo" : s.license}
                          </span>
                        ) : null}
                      </div>
                      <footer>
                        {have.length ? <span className="library-have">Added to {have.map((h) => agentName(h.agent_id)).join(", ")}</span> : <span />}
                        <button className={`btn small${here ? " secondary" : ""}`} type="button" disabled={!!busy || !!s.error} onClick={() => add(s.path)}>
                          {busy === `add:${s.path}` ? "Adding…" : here ? "Update" : <><IconPlus size={14} /> Add</>}
                        </button>
                      </footer>
                    </article>
                  );
                })}
              </div>
              {catalog && shown.length === 0 ? <p className="muted">Nothing matches.</p> : null}
            </>
          ) : null}

          {tab === "upload" ? (
            <label
              className={`skill-drop${drag ? " drag" : ""}`}
              onDragOver={(e) => {
                e.preventDefault();
                setDrag(true);
              }}
              onDragLeave={() => setDrag(false)}
              onDrop={(e) => {
                e.preventDefault();
                setDrag(false);
                upload(e.dataTransfer.files?.[0]);
              }}
            >
              <span className="showcase-icon">
                <IconSkills size={24} />
              </span>
              <strong>{busy === "upload" ? "Importing…" : "Drop a SKILL.md or a .zip of skill folders"}</strong>
              <span className="muted">Or click to choose a file. A .zip can hold up to 25 skills; each keeps its text files (references, templates, scripts).</span>
              <input ref={fileRef} type="file" accept=".md,.zip,text/markdown,application/zip" onChange={(e) => upload(e.target.files?.[0])} disabled={busy === "upload"} />
            </label>
          ) : null}

          {tab === "mine" ? (
            skills.length === 0 ? (
              <p className="muted">No skills yet. Add one from a library, upload one, or teach one from an agent's profile.</p>
            ) : (
              <div className="vault-list">
                {skills.map((s) => (
                  <div key={s.id} className="vault-item skill-item">
                    <span className="vault-monogram" aria-hidden>
                      {s.name.slice(0, 1).toUpperCase()}
                    </span>
                    <button type="button" className="vault-item-main skill-open" onClick={() => setOpen(s.id)}>
                      <strong>{s.name}</strong>
                      <span className="vault-item-meta">
                        <span className={`kind-chip kind-${s.kind}`}>{kindLabel(s)}</span>
                        <span>{agentName(s.agent_id)}</span>
                        {s.source?.label ? <span className="faint">{s.source.label.replace(/^an uploaded file \((.*)\)$/, "$1")}</span> : null}
                        {s.updated_at || s.created_at ? <span className="faint">{relativeTime(s.updated_at || s.created_at)}</span> : null}
                      </span>
                    </button>
                    <div className="vault-actions">
                      {s.source?.type === "github" ? (
                        <button className="btn secondary small" type="button" disabled={!!busy} onClick={() => checkUpdate(s)}>
                          {busy === `upd:${s.id}` ? "Checking…" : "Check for update"}
                        </button>
                      ) : null}
                      <a className="btn secondary small" href={skillExportUrl(s.id)} download>
                        Export
                      </a>
                    </div>
                  </div>
                ))}
              </div>
            )
          ) : null}

          <p className="skills-trust">
            <IconLock size={14} /> Imported skills are someone else's instructions. Lumio keeps their text files only, never installs or runs
            their scripts on its own, and your approvals still apply to everything an agent does with them.
            {imported ? ` You have ${imported} imported.` : ""}
          </p>
        </div>
      </div>
      {open ? <SkillDetail skillId={open} onClose={() => setOpen(null)} onChanged={() => void refreshSkills()} /> : null}
    </AppShell>
  );
}
