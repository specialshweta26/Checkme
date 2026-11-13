"use client";

import { FormEvent, useEffect, useState } from "react";
import { AppShell } from "../components/AppShell";
import { AgentAvatar } from "../components/AgentAvatar";
import { chatHref, getMe, relativeTime, rememberScope, type Me } from "../lib";

type AgentOption = { id: string; name: string; title?: string; color?: string; avatar?: string };

type TodayItem = {
  id: string;
  kind: string;
  agent_id?: string | null;
  agent_name: string;
  agent_color?: string;
  agent_avatar?: string;
  title: string;
  detail: string;
  at?: string | null;
  when?: string;
  href: string;
};

type TodayBoard = {
  greeting: string;
  name: string;
  date_label: string;
  default_agent_id?: string | null;
  agents: AgentOption[];
  counts: { working: number; needs_you: number; done_today: number; coming_up: number };
  needs_you: TodayItem[];
  working: TodayItem[];
  done_today: TodayItem[];
  coming_up: TodayItem[];
};

const EMPTY: TodayBoard = {
  greeting: "Good evening",
  name: "",
  date_label: "",
  agents: [],
  counts: { working: 0, needs_you: 0, done_today: 0, coming_up: 0 },
  needs_you: [],
  working: [],
  done_today: [],
  coming_up: [],
};

export default function TodayPage() {
  const [me, setMe] = useState<Me | null>(null);
  const [board, setBoard] = useState<TodayBoard>(EMPTY);
  const [agentId, setAgentId] = useState("");
  const [text, setText] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    getMe().then((user) => {
      if (!user) {
        window.location.replace("/login");
        return;
      }
      setMe(user);
    });
  }, []);

  useEffect(() => {
    if (!me) return;
    let stop = false;
    async function load() {
      const res = await fetch("/api/today", { credentials: "include" });
      if (res.status === 401) {
        window.location.replace("/login");
        return;
      }
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        if (!stop) setError(typeof data.detail === "string" ? data.detail : "Could not load today");
        return;
      }
      if (stop) return;
      setError("");
      setBoard(data);
      setAgentId((current) => current || data.default_agent_id || data.agents?.[0]?.id || "");
    }
    load();
    const timer = window.setInterval(load, 15000);
    return () => {
      stop = true;
      window.clearInterval(timer);
    };
  }, [me]);

  function giveTask(e: FormEvent) {
    e.preventDefault();
    const task = text.trim();
    const id = agentId || board.default_agent_id || board.agents[0]?.id;
    if (!task || !id) return;
    window.sessionStorage.setItem("lumio.pendingTask", JSON.stringify({ agentId: id, text: task }));
    rememberScope(id, null);
    window.location.assign(chatHref({ agent: id }));
  }

  const counts = board.counts || EMPTY.counts;

  return (
    <AppShell me={me} current="today">
      <div className="today">
        <header className="today-head">
          <p className="today-date">{board.date_label}</p>
          <h1>
            {board.greeting}
            {board.name ? `, ${board.name}` : ""}
          </h1>
          <p className="today-counts">
            <span>{counts.working} working</span>
            <span>{counts.needs_you} need you</span>
            <span>{counts.done_today} done today</span>
          </p>
        </header>

        <form className="today-composer" onSubmit={giveTask}>
          <label className="today-agent">
            <span className="sr">Bot</span>
            <select value={agentId} onChange={(e) => setAgentId(e.target.value)} aria-label="Bot">
              {board.agents.map((agent) => (
                <option key={agent.id} value={agent.id}>
                  {agent.name}
                </option>
              ))}
            </select>
          </label>
          <input
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="Give a bot a task…"
            aria-label="Give a bot a task"
          />
          <button type="submit" aria-label="Send task" disabled={!text.trim() || !agentId}>
            <Arrow />
          </button>
        </form>
        {error ? <p className="today-error">{error}</p> : null}

        <div className="today-grid">
          <Section
            kicker="Needs you"
            count={counts.needs_you}
            icon="spark"
            empty="Nothing needs you right now."
            items={board.needs_you}
          />
          <Section
            kicker="Working now"
            count={counts.working}
            icon="live"
            empty="No bot is working right now."
            items={board.working}
            working
          />
          <Section
            kicker="Done today"
            count={counts.done_today}
            icon="done"
            empty="Nothing finished yet today."
            items={board.done_today}
          />
          <Section
            kicker="Coming up"
            count={counts.coming_up}
            icon="clock"
            empty="No routines are set."
            items={board.coming_up}
          />
        </div>
      </div>
    </AppShell>
  );
}

function Section({
  kicker,
  count,
  icon,
  empty,
  items,
  working = false,
}: {
  kicker: string;
  count: number;
  icon: "spark" | "live" | "done" | "clock";
  empty: string;
  items: TodayItem[];
  working?: boolean;
}) {
  return (
    <section className="today-card">
      <header>
        <Mark icon={icon} />
        <h2>{kicker}</h2>
        {count > 0 ? <span className="today-count">{count}</span> : null}
      </header>
      {items.length === 0 ? (
        <p className="today-empty">{empty}</p>
      ) : (
        <ul>
          {items.map((item) => (
            <li key={item.id}>
              <a href={item.href}>
                <AgentAvatar
                  name={item.agent_name || item.title}
                  color={item.agent_color}
                  avatar={item.agent_avatar}
                  working={working}
                  className="agent-avatar today-face"
                />
                <span>
                  <strong>{item.title}</strong>
                  {item.detail ? <small>{item.detail}</small> : null}
                </span>
                <time>{item.when || relativeTime(item.at || undefined)}</time>
              </a>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function Mark({ icon }: { icon: "spark" | "live" | "done" | "clock" }) {
  if (icon === "live") {
    return (
      <svg className="today-mark live" width="16" height="16" viewBox="0 0 16 16" aria-hidden>
        <circle cx="8" cy="8" r="3" fill="currentColor" />
      </svg>
    );
  }
  if (icon === "done") {
    return (
      <svg className="today-mark done" width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden>
        <circle cx="12" cy="12" r="8" stroke="currentColor" strokeWidth="1.8" />
        <path d="M8.5 12.2 11 14.6 15.5 9.6" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
      </svg>
    );
  }
  if (icon === "clock") {
    return (
      <svg className="today-mark clock" width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden>
        <circle cx="12" cy="12" r="8" stroke="currentColor" strokeWidth="1.8" />
        <path d="M12 8.5V12l2.4 1.6" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
      </svg>
    );
  }
  return (
    <svg className="today-mark spark" width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden>
      <path d="M12 3.5 13.4 9 19 10.5 13.4 12 12 17.5 10.6 12 5 10.5 10.6 9 12 3.5Z" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" />
    </svg>
  );
}

function Arrow() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden>
      <path d="M12 18V6M6.5 11.5 12 6l5.5 5.5" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}
