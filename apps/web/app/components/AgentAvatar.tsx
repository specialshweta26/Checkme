import type { CSSProperties } from "react";
import { mascotFilter, mascotFor } from "./mascots";

export function AgentAvatar({
  name,
  color,
  avatar,
  className = "agent-avatar",
}: {
  name: string;
  color?: string;
  avatar?: string;
  className?: string;
}) {
  const mascot = mascotFor(avatar);
  if (mascot) {
    // One of the twelve agent mascots, tinted to the agent's color.
    return (
      <span className={`${className} mascot`} aria-hidden="true">
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
