// Message times and day dividers in the chat, like a messaging app: "14:05" under each message and a "Today",
// "Yesterday", "Monday" or "18 November" line where the day changes. Times are the viewer's own clock.

type Timed = { role: string; meta?: any };

function parse(at: unknown): Date | null {
  if (!at) return null;
  const d = new Date(String(at));
  return Number.isNaN(d.getTime()) ? null : d;
}

function dayStart(d: Date): number {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
}

/** "14:05" (or "2:05 PM" where the viewer's locale uses a 12-hour clock). */
export function msgTime(at: unknown): string {
  const d = parse(at);
  return d ? d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "";
}

/** Full date and time, for the tooltip. */
export function msgFull(at: unknown): string {
  const d = parse(at);
  return d ? d.toLocaleString([], { dateStyle: "full", timeStyle: "short" }) : "";
}

/** "Today", "Yesterday", a weekday within the last week, else "18 November" (with the year when it isn't this one). */
export function dayLabel(at: unknown, now: Date = new Date()): string {
  const d = parse(at);
  if (!d) return "";
  const days = Math.round((dayStart(now) - dayStart(d)) / 86_400_000);
  if (days === 0) return "Today";
  if (days === 1) return "Yesterday";
  if (days > 1 && days < 7) return d.toLocaleDateString([], { weekday: "long" });
  return d.toLocaleDateString([], {
    day: "numeric",
    month: "long",
    ...(d.getFullYear() === now.getFullYear() ? {} : { year: "numeric" }),
  });
}

/** The first timed message of each day gets a divider above it. */
export function startsNewDay(messages: Timed[], i: number): boolean {
  const d = parse(messages[i]?.meta?.at);
  if (!d) return false;
  for (let j = i - 1; j >= 0; j -= 1) {
    const prev = parse(messages[j]?.meta?.at);
    if (prev) return dayStart(prev) !== dayStart(d);
  }
  return true;
}

/** How long a reply took after the message before it: "12s", "3 min", "1 h 5 min". Empty when unknown. */
export function replyDelay(messages: Timed[], i: number): string {
  const end = parse(messages[i]?.meta?.at);
  if (!end) return "";
  for (let j = i - 1; j >= 0; j -= 1) {
    if (messages[j].role !== "user") continue;
    const start = parse(messages[j].meta?.at);
    if (!start) return "";
    const s = Math.round((end.getTime() - start.getTime()) / 1000);
    if (s < 1) return "";
    if (s < 60) return `${s}s`;
    const m = Math.round(s / 60);
    if (m < 60) return `${m} min`;
    return `${Math.floor(m / 60)} h${m % 60 ? ` ${m % 60} min` : ""}`;
  }
  return "";
}
