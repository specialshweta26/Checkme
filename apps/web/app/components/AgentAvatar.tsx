import type { CSSProperties } from "react";
import { mascotFilter, mascotFor } from "./mascots";

export function AgentAvatar({
  name,
  color,
  avatar,
  working = false,
  className = "agent-avatar",
}: {
  name: string;
  color?: string;
  avatar?: string;
  /** Bob and glow while this agent is in the middle of a turn. */
  working?: boolean;
  className?: string;
}) {
  const mascot = mascotFor(avatar);
  if (mascot) {
    // The artwork is the character only. Color tints it; working adds the live mascot motion.
    const style = { "--mascot": color || "#f97316" } as CSSProperties;
    return (
      <span className={`${className} mascot${working ? " working" : ""}`} style={style} aria-hidden="true">
        <img src={mascot.src} alt="" draggable={false} style={{ filter: mascotFilter(color) }} />
      </span>
    );
  }
  const image = avatar?.startsWith("data:image/") ? avatar : "";
  const style: CSSProperties = {
    backgroundColor: color || "#f97316",
    ...(image ? { backgroundImage: `url("${image}")`, backgroundSize: "cover", backgroundPosition: "center" } : {}),
  };
  return (
    <span className={className} style={style} aria-hidden="true">
      {image ? null : avatar || (name || "?").slice(0, 1).toUpperCase()}
    </span>
  );
}
