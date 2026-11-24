"use client";

// What a model can do (apps/api/app/model_keys/capabilities.py) and OpenAI's processing tiers, shared by the Models
// page and an agent's Model section. Unknown abilities aren't shown; a model known not to call tools can't run agents.

export type Caps = { tools?: boolean; images?: boolean; pdf?: boolean; audio?: boolean; context?: number; max_output?: number };
export type Tier = { key: string; name: string };

export const TIER_HINT: Record<string, string> = {
  "": "Standard price and speed.",
  flex: "About half the price, slower; sometimes busy, then Lumio uses Standard for a while.",
  priority: "Faster replies at a higher price.",
  ultrafast: "Fastest replies at the highest price, where your account and model offer it; otherwise Standard.",
};

const tokens = (n: number) => (n >= 1_000_000 ? `${(n / 1_000_000).toFixed(n % 1_000_000 ? 1 : 0)}M` : `${Math.round(n / 1000)}k`);

export function capsText(caps?: Caps): string {
  if (!caps) return "";
  const parts: string[] = [];
  if (caps.tools === false) parts.push("no tools");
  else if (caps.tools) parts.push("tools");
  if (caps.images) parts.push("images");
  if (caps.pdf) parts.push("PDF");
  if (caps.audio) parts.push("audio");
  if (caps.context) parts.push(`${tokens(caps.context)} context`);
  return parts.join(" · ");
}

export function CapsLine({ caps, model }: { caps?: Caps; model: string }) {
  if (!model) return null;
  if (!caps || !capsText(caps)) return <em className="models-caps">Lumio doesn't know this model's abilities yet; it learns from its errors.</em>;
  return (
    <em className={`models-caps${caps.tools === false ? " bad" : ""}`}>
      {capsText(caps)}
      {caps.tools === false ? " — can't run agents" : caps.images === false ? " — images are described to it" : ""}
    </em>
  );
}

// Reasoning for any model (docs/LLD.md "Reasoning"): OpenAI's effort, Anthropic's extended thinking, reasoning_effort on
// other providers. "Default" keeps Lumio's setting, which reasons more on its own once a turn works with connector data.
export const REASONING_HINT: Record<string, string> = {
  "": "Lumio decides: quick answers fast, more thinking when it works through your data.",
  none: "No extra thinking: fastest and cheapest.",
  low: "A little thinking before acting.",
  medium: "Thinks things through: better at multi-step data questions, a few seconds slower.",
  high: "Thinks longest: hardest questions, slowest and most tokens.",
};

export function ReasoningSelect({ levels, value, onChange, disabled }: { levels: Tier[]; value: string; onChange: (v: string) => void; disabled?: boolean }) {
  return (
    <>
      <select value={value} disabled={disabled} onChange={(e) => onChange(e.target.value)} aria-label="Reasoning">
        {levels.map((t) => (
          <option key={t.key} value={t.key}>
            {t.name}
          </option>
        ))}
      </select>
      <em className="models-caps">{REASONING_HINT[value] || ""}</em>
    </>
  );
}

export function TierSelect({ tiers, value, onChange, disabled }: { tiers: Tier[]; value: string; onChange: (v: string) => void; disabled?: boolean }) {
  return (
    <>
      <select value={value} disabled={disabled} onChange={(e) => onChange(e.target.value)} aria-label="Processing tier">
        {tiers.map((t) => (
          <option key={t.key} value={t.key}>
            {t.name}
          </option>
        ))}
      </select>
      <em className="models-caps">{TIER_HINT[value] || ""}</em>
    </>
  );
}
