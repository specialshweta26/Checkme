"use client";

/** Options for a new agent's first screen. Keys match apps/api/app/agents/focus.py (FOCUS_ORDER). */
export const FOCUS_OPTIONS = [
  { key: "email_calendar", label: "Email and calendar", answer: "I'd most like a hand with email and calendar." },
  { key: "coding", label: "Coding and deployments", answer: "I'd most like a hand with coding and deployments." },
  { key: "research", label: "Research and writing", answer: "I'd most like a hand with research and writing." },
  { key: "errands", label: "Errands and bookings", answer: "I'd most like a hand with errands and bookings." },
] as const;

export type FocusOption = (typeof FOCUS_OPTIONS)[number];

const DISMISS_KEY = "checkme.focusCard.dismissed";

export function focusCardDismissed(agentId: string) {
  if (typeof window === "undefined") return false;
  return window.localStorage.getItem(`${DISMISS_KEY}.${agentId}`) === "1";
}

export function dismissFocusCard(agentId: string) {
  window.localStorage.setItem(`${DISMISS_KEY}.${agentId}`, "1");
}

/**
 * Greeting + "What would you most like a hand with?" card shown on a new agent's empty chat.
 * Picking an option stores it as the agent's focus and sends it as the user's answer.
 */
export function FocusOnboarding({
  userName,
  agentName,
  agentColor,
  disabled,
  onPick,
  onDismiss,
}: {
  userName: string;
  agentName: string;
  agentColor?: string;
  disabled?: boolean;
  onPick: (option: FocusOption) => void;
  onDismiss: () => void;
}) {
  return (
    <div className="onboard">
      <div className="turn assistant">
        <div className="agent-chip">
          <span className="agent-avatar tiny" style={{ background: agentColor || "#f97316" }}>
            {agentName.slice(0, 1)}
          </span>
          {agentName}
        </div>
        <div className="onboard-hello">
          Hi {userName}, I&apos;m {agentName}. Happy to be here, and I&apos;d love to get a sense of how I can make your days
          easier.
        </div>
      </div>
      <div className="focus-card" role="group" aria-label="What would you most like a hand with?">
        <div className="focus-card-head">
          <strong>What would you most like a hand with?</strong>
          <button type="button" className="focus-card-close" aria-label="Dismiss" onClick={onDismiss}>
            ×
          </button>
        </div>
        <ul>
          {FOCUS_OPTIONS.map((opt, i) => (
            <li key={opt.key}>
              <button type="button" className="focus-option" disabled={disabled} onClick={() => onPick(opt)}>
                <span className="focus-letter">{String.fromCharCode(65 + i)}</span>
                {opt.label}
              </button>
            </li>
          ))}
        </ul>
      </div>
      <p className="focus-hint">Or answer in the chat below</p>
    </div>
  );
}
