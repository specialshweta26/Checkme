"use client";

import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import {
  AgentSkill,
  deleteSkill,
  describeSkillStep,
  getSkill,
  rewriteSkill,
  skillBusy,
  skillFrameUrl,
  updateSkill,
} from "../lib";

function clock(t?: number) {
  const s = Math.max(0, Math.floor(t || 0));
  return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}

export function SkillStatusChip({ skill }: { skill: AgentSkill }) {
  if (skill.status === "recording") return <span className="skill-chip busy">recording…</span>;
  if (skill.status === "writing") return <span className="skill-chip busy">writing skill…</span>;
  if (skill.error) return <span className="skill-chip warn">write-up failed</span>;
  return null;
}

/** Full view of one skill: rename, edit instructions, steps, narration, screenshots, retry and delete. */
export function SkillDetail({
  skillId,
  onClose,
  onChanged,
}: {
  skillId: string;
  onClose: () => void;
  onChanged: () => void;
}) {
  const [skill, setSkill] = useState<AgentSkill | null>(null);
  const [missing, setMissing] = useState(false);
  const [name, setName] = useState("");
  const [instruction, setInstruction] = useState("");
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [confirmDelete, setConfirmDelete] = useState(false);

  async function load(keepEdits: boolean) {
    try {
      const row = await getSkill(skillId);
      if (!row) {
        setMissing(true);
        return;
      }
      setSkill(row);
      if (!keepEdits) {
        setName(row.name);
        setInstruction(row.instruction || "");
      }
    } catch (err: any) {
      setError(err.message || "Could not load the skill");
    }
  }

  useEffect(() => {
    void load(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [skillId]);

  // While it is recording or being written up, refresh every few seconds (never blocks the view).
  useEffect(() => {
    if (!skillBusy(skill)) return;
    const timer = window.setInterval(() => void load(dirty), 3000);
    return () => window.clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [skill?.status, dirty]);

  useEffect(() => {
    if (skill && !skillBusy(skill) && !dirty) {
      setName(skill.name);
      setInstruction(skill.instruction || "");
    }
  }, [skill, dirty]);

  async function save() {
    if (!skill) return;
    setBusy(true);
    setError("");
    try {
      const row = await updateSkill(skill.id, { name: name.trim() || skill.name, instruction });
      setSkill({ ...skill, ...row });
      setDirty(false);
      onChanged();
    } catch (err: any) {
      setError(err.message || "Could not save");
    } finally {
      setBusy(false);
    }
  }

  async function retry() {
    if (!skill) return;
    setBusy(true);
    setError("");
    try {
      const row = await rewriteSkill(skill.id);
      setSkill({ ...skill, ...row });
      setDirty(false);
      onChanged();
    } catch (err: any) {
      setError(err.message || "Could not restart the write-up");
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    if (!skill) return;
    setBusy(true);
    try {
      await deleteSkill(skill.id);
      onChanged();
      onClose();
    } catch (err: any) {
      setError(err.message || "Could not delete");
      setBusy(false);
    }
  }

  const steps = (skill?.steps || []).filter((s) => s.kind !== "say");
  const view = (
    <div className="sheet skill-sheet" role="dialog" aria-modal="true" aria-label="Skill" onClick={onClose}>
      <div className="panel skill-detail" onClick={(e) => e.stopPropagation()}>
        <div className="skill-detail-head">
          <strong>{skill ? "Skill" : missing ? "Skill not found" : "Loading…"}</strong>
          <button className="btn secondary" type="button" onClick={onClose} aria-label="Close">
            Close
          </button>
        </div>
        {missing ? <p className="muted">This skill no longer exists (nothing was recorded, or it was deleted).</p> : null}
        {skill ? (
          <>
            <p className="muted">
              {skill.kind === "taught" ? "Taught by demonstration" : "Saved workflow"}
              {skill.duration_ms ? ` · ${clock(skill.duration_ms / 1000)} recorded` : ""}
              {skill.step_count ? ` · ${steps.length} steps` : ""} <SkillStatusChip skill={skill} />
            </p>
            {skill.status === "recording" ? <p className="muted">Recording. Steps are saved as you go.</p> : null}
            {skill.status === "writing" ? <p className="muted">Writing the skill from your demonstration. The recorded steps below are already saved.</p> : null}
            {skill.error ? (
              <div className="skill-error">
                <p>{skill.error}</p>
                <button className="btn secondary" type="button" disabled={busy} onClick={retry}>
                  Retry write-up
                </button>
              </div>
            ) : null}
            <label>
              Name
              <input
                value={name}
                maxLength={160}
                onChange={(e) => {
                  setName(e.target.value);
                  setDirty(true);
                }}
              />
            </label>
            <label>
              Instructions
              <textarea
                rows={8}
                value={instruction}
                onChange={(e) => {
                  setInstruction(e.target.value);
                  setDirty(true);
                }}
                placeholder={skill.status === "writing" ? "Being written…" : "What the agent should do when this skill runs"}
              />
            </label>
            <div className="agent-form-actions">
              <button className="btn" type="button" disabled={busy || !dirty} onClick={save}>
                Save
              </button>
              {skill.kind === "taught" && (skill.step_count || skill.has_transcript) ? (
                <button className="btn secondary" type="button" disabled={busy || skill.status === "writing" || skill.status === "recording"} onClick={retry}>
                  Re-run write-up
                </button>
              ) : null}
              {confirmDelete ? (
                <>
                  <button className="btn danger" type="button" disabled={busy} onClick={remove}>
                    Delete for good
                  </button>
                  <button className="btn secondary" type="button" onClick={() => setConfirmDelete(false)}>
                    Keep
                  </button>
                </>
              ) : (
                <button className="btn secondary" type="button" disabled={busy} onClick={() => setConfirmDelete(true)}>
                  Delete
                </button>
              )}
            </div>
            {error ? <p className="error">{error}</p> : null}
            {steps.length ? (
              <details open={!skill.instruction}>
                <summary>Recorded steps ({steps.length})</summary>
                <ol className="skill-steps">
                  {steps.map((step, i) => (
                    <li key={i}>
                      <span className="muted">{clock(step.t)}</span> {describeSkillStep(step)}
                      {step.said ? <em className="muted"> — “{step.said}”</em> : null}
                    </li>
                  ))}
                </ol>
              </details>
            ) : null}
            {skill.transcript?.length ? (
              <details>
                <summary>Narration ({skill.transcript.length})</summary>
                <ul className="skill-steps">
                  {skill.transcript.map((seg, i) => (
                    <li key={i}>
                      <span className="muted">{clock(seg.t)}</span> {seg.text}
                    </li>
                  ))}
                </ul>
              </details>
            ) : null}
            {skill.frames?.length ? (
              <details>
                <summary>Screenshots ({skill.frames.length})</summary>
                <div className="skill-frames">
                  {skill.frames.map((f) => (
                    <a key={f.index} href={skillFrameUrl(skill.id, f.index)} target="_blank" rel="noreferrer">
                      {/* eslint-disable-next-line @next/next/no-img-element */}
                      <img src={skillFrameUrl(skill.id, f.index)} alt={`Screen at ${clock(f.t)}`} loading="lazy" />
                      <span className="muted">{clock(f.t)}</span>
                    </a>
                  ))}
                </div>
              </details>
            ) : null}
            {skill.notes?.length ? <p className="muted">Notes: {skill.notes.join("; ")}</p> : null}
          </>
        ) : null}
      </div>
    </div>
  );
  return typeof document === "undefined" ? view : createPortal(view, document.body);
}
