"use client";

import { useEffect, useState } from "react";

const PHASES = ["Thinking", "Checking connected sources", "Gathering the answer", "Writing a reply"];

export function Thinking() {
  const [i, setI] = useState(0);
  useEffect(() => {
    const t = window.setInterval(() => setI((n) => (n + 1) % PHASES.length), 2400);
    return () => window.clearInterval(t);
  }, []);
  return (
    <div className="turn assistant" aria-live="polite" aria-label="Assistant is thinking">
      <div className="think">
        <span className="think-orb" />
        <span className="think-copy">{PHASES[i]}</span>
      </div>
      <div className="think-bars">
        <div className="bar w1" />
        <div className="bar w2" />
        <div className="bar w3" />
      </div>
    </div>
  );
}
