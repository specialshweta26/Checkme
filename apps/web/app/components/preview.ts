/** Human-readable text for an approval card preview (mail, calendar, git push / PR / merge). */
export function formatPreview(preview: any): string {
  if (!preview) return "No preview.";
  if (typeof preview === "string") return preview;
  if (preview.repo) {
    const rows: string[] = [`Repo: ${preview.repo}`];
    if (preview.number != null) rows.push(`Pull request: #${preview.number}${preview.method ? ` (${preview.method})` : ""}`);
    if (preview.branch) rows.push(`Branch: ${preview.branch}${preview.base ? ` → ${preview.base}` : ""}`);
    if (preview.head) rows.push(`Head: ${preview.head} → ${preview.base || "default branch"}`);
    if (preview.title) rows.push(`Title: ${preview.title}`);
    if (preview.message) rows.push(`Commit: ${preview.message}`);
    if (preview.files != null) rows.push(`Files: ${preview.files}`);
    if (preview.open_pr) rows.push("Also opens a pull request");
    if (Array.isArray(preview.commits) && preview.commits.length) rows.push("Commits:", ...preview.commits.map((c: string) => `  ${c}`));
    if (preview.diffstat) rows.push(preview.diffstat);
    return rows.join("\n");
  }
  const order = ["folder", "count", "to", "cc", "bcc", "reply_all", "recipient", "subject", "when", "schedule", "body", "preview", "text", "sample", "reason", "instruction"];
  const lines = order
    .filter((k) => preview[k] != null && preview[k] !== "")
    .map((k) => {
      const label = k[0].toUpperCase() + k.slice(1);
      const value = typeof preview[k] === "string" ? preview[k] : JSON.stringify(preview[k]);
      return `${label}: ${value}`;
    });
  return lines.length ? lines.join("\n") : JSON.stringify(preview, null, 2);
}
