import type { CSSProperties } from "react";

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
