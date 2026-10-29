"use client";

import { useEffect, useState } from "react";
import { getAgentOnboarding, type AgentOnboarding } from "../lib";
import { AgentAvatar } from "./AgentAvatar";

const DISMISS_KEY = "checkme.agentOnboarding.dismissed";

export function focusCardDismissed(agentId: string) {
  if (typeof window === "undefined") return false;
  return window.localStorage.getItem(`${DISMISS_KEY}.${agentId}`) === "1";
}

export function dismissFocusCard(agentId: string) {
  window.localStorage.setItem(`${DISMISS_KEY}.${agentId}`, "1");
}

export function FocusOnboarding({
  agentId,
  userName,
  agentName,
  agentTitle,
  agentColor,
  agentAvatar,
  disabled,
  onPick,
  onDismiss,
}: {
  agentId: string;
  userName: string;
  agentName: string;
  agentTitle?: string;
  agentColor?: string;
  agentAvatar?: string;
  disabled?: boolean;
  onPick: (prompt: string) => void;
  onDismiss: () => void;
}) {
  const [data, setData] = useState<AgentOnboarding | null>(null);
  useEffect(() => {
    let alive = true;
    getAgentOnboarding(agentId)
      .then((result) => { if (alive) setData(result); })
      .catch(() => { if (alive) setData(null); });
    return () => { alive = false; };
  }, [agentId]);

  return (
    <div className="onboard">
      <div className="turn assistant">
        <div className="agent-chip">
          <AgentAvatar name={agentName} color={agentColor} avatar={agentAvatar} className="agent-avatar tiny" />
          {agentName}
        </div>
        <div className="onboard-hello">
          {data?.greeting || `Hi ${userName}, I’m ${agentName}${agentTitle ? `, your ${agentTitle}` : ""}. I’m getting a few ideas ready for how I can help.`}
        </div>
      </div>
      <div className="focus-card" role="group" aria-label={`Getting started with ${agentName}`}>
        <div className="focus-card-head">
          <strong>{data ? "A few things we can shape together" : "Preparing ideas for this agent…"}</strong>
          <button type="button" className="focus-card-close" aria-label="Dismiss" onClick={onDismiss}>×</button>
        </div>
        {data?.questions.length ? (
          <ul className="agent-onboard-questions">
            {data.questions.map((question) => <li key={question}>{question}</li>)}
          </ul>
        ) : null}
        {data?.tasks.length ? (
          <>
            <p className="agent-onboard-label">Possible first tasks</p>
            <div className="agent-onboard-actions">
              {data.tasks.map((task) => (
                <button key={task.title} type="button" className="focus-option" disabled={disabled} onClick={() => onPick(task.prompt)}>
                  <span className="focus-letter">↗</span><span><b>{task.title}</b><small>{task.prompt}</small></span>
                </button>
              ))}
            </div>
          </>
        ) : null}
        {data?.skills.length ? (
          <>
            <p className="agent-onboard-label">Skills this agent could learn</p>
            <div className="agent-onboard-actions">
              {data.skills.map((skill) => (
                <button key={skill.title} type="button" className="focus-option" disabled={disabled} onClick={() => onPick(skill.prompt)}>
                  <span className="focus-letter">✦</span><span><b>{skill.title}</b><small>{skill.prompt}</small></span>
                </button>
              ))}
            </div>
          </>
        ) : null}
      </div>
      <p className="focus-hint">You can choose an idea, answer one of the questions, or tell {agentName} what you need in your own words.</p>
    </div>
  );
}
