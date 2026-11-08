// Agent icons: the twelve Lumio mascots (apps/web/public/agents). An agent's avatar is "mascot:01" … "mascot:12";
// its color tints the mascot (the artwork is orange, so a color becomes a hue shift; greys and black desaturate it).

export type Mascot = { id: string; src: string; label: string };

const NAMES = ["wave", "support", "thinker", "cheer", "calm", "presenter", "badge", "puzzled", "proud", "serene", "excited", "curious"];
const LABELS = ["Waving", "Support", "Thinker", "Cheering", "Calm", "Presenter", "Team badge", "Puzzled", "Proud", "Serene", "Excited", "Curious"];

export const MASCOTS: Mascot[] = NAMES.map((name, i) => {
  const n = String(i + 1).padStart(2, "0");
  return { id: `mascot:${n}`, src: `/agents/${n}-${name}.png`, label: LABELS[i] };
});

export function mascotFor(avatar?: string): Mascot | undefined {
  return MASCOTS.find((m) => m.id === avatar);
}

export function randomMascot(): Mascot {
  return MASCOTS[Math.floor(Math.random() * MASCOTS.length)];
}

const BASE_HUE = 25; // the artwork's orange

function hsl(hex: string): [number, number, number] | null {
  const m = /^#?([0-9a-f]{6})$/i.exec((hex || "").trim());
  if (!m) return null;
  const n = parseInt(m[1], 16);
  const r = ((n >> 16) & 255) / 255;
  const g = ((n >> 8) & 255) / 255;
  const b = (n & 255) / 255;
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  const l = (max + min) / 2;
  if (max === min) return [0, 0, l];
  const d = max - min;
  const s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
  let h = max === r ? (g - b) / d + (g < b ? 6 : 0) : max === g ? (b - r) / d + 2 : (r - g) / d + 4;
  h *= 60;
  return [h, s, l];
}

const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));

/** A CSS filter that turns the orange mascot into roughly ``color``. */
export function mascotFilter(color?: string): string {
  const c = hsl(color || "#f97316");
  if (!c) return "none";
  const [h, s, l] = c;
  if (s < 0.12) {
    // Black, greys: a neutral mascot, darker for darker swatches.
    return `grayscale(1) brightness(${clamp(0.45 + l * 0.9, 0.5, 1.2).toFixed(2)}) contrast(1.05)`;
  }
  const shift = Math.round(h - BASE_HUE);
  const sat = clamp(s / 0.95, 0.35, 1.5);
  const bright = clamp(0.5 + l, 0.6, 1.15);
  return `hue-rotate(${shift}deg) saturate(${sat.toFixed(2)}) brightness(${bright.toFixed(2)})`;
}
