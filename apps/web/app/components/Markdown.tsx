"use client";

import { Fragment, useState, type ReactNode } from "react";
import { DataTable, textNodes } from "./DataTable";

function inline(text: string): ReactNode[] {
  const parts = text.split(/(\*\*[^*]+\*\*|\*[^*]+\*|`[^`]+`|!\[[^\]]*\]\([^)]+\)|\[[^\]]+\]\([^)]+\))/g);
  return parts.map((part, i) => {
    if (part.startsWith("**") && part.endsWith("**")) return <strong key={i}>{part.slice(2, -2)}</strong>;
    if (part.startsWith("*") && part.endsWith("*")) return <em key={i}>{part.slice(1, -1)}</em>;
    if (part.startsWith("`") && part.endsWith("`")) return <code key={i}>{part.slice(1, -1)}</code>;
    const image = part.match(/^!\[([^\]]*)\]\((https?:[^)]+)\)$/);
    if (image) {
      return <img key={i} src={image[2]} alt={image[1]} />;
    }
    const link = part.match(/^\[([^\]]+)\]\((https?:[^)]+)\)$/);
    if (link) {
      return (
        <a key={i} href={link[2]} target="_blank" rel="noreferrer">
          {link[1]}
        </a>
      );
    }
    return <Fragment key={i}>{part}</Fragment>;
  });
}

function isTable(lines: string[]) {
  return lines.length >= 2 && lines.every((r) => r.includes("|")) && /\|?\s*-{2,}/.test(lines[1] || "");
}

function table(lines: string[], key: string) {
  const rows = lines
    .filter((r) => !/^\s*\|?\s*-{2,}/.test(r))
    .map((r) =>
      r
        .replace(/^\|/, "")
        .replace(/\|$/, "")
        .split("|")
        .map((c) => c.trim()),
    );
  const head = rows[0] || [];
  const body = rows.slice(1);
  return <DataTable key={key} head={head} body={body} cell={(text) => textNodes(text)} />;
}

function CodeBlock({ body }: { body: string }) {
  const [copied, setCopied] = useState(false);
  async function copy() {
    try {
      await navigator.clipboard.writeText(body);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1400);
    } catch {
      setCopied(false);
    }
  }
  return (
    <div className="code-block">
      <button type="button" className="code-copy" onClick={copy} aria-label="Copy code">
        {copied ? "Copied" : "Copy"}
      </button>
      <pre><code>{body}</code></pre>
    </div>
  );
}

function flushParagraph(buf: string[], out: ReactNode[], key: string) {
  if (!buf.length) return;
  out.push(
    <p key={key}>
      {buf.map((line, i) => (
        <Fragment key={i}>
          {i ? <br /> : null}
          {inline(line)}
        </Fragment>
      ))}
    </p>,
  );
  buf.length = 0;
}

function renderSegment(text: string, prefix: string): ReactNode[] {
  const lines = text.replace(/\r\n/g, "\n").split("\n");
  const out: ReactNode[] = [];
  let buf: string[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const heading = line.match(/^(#{1,3})\s+(.+)$/);
    if (heading) {
      flushParagraph(buf, out, `${prefix}p${i}`);
      const Tag = (`h${heading[1].length}` as unknown) as "h1" | "h2" | "h3";
      out.push(<Tag key={`${prefix}h${i}`}>{inline(heading[2])}</Tag>);
      i += 1;
      continue;
    }
    if (line.startsWith("|") || (line.includes("|") && lines[i + 1] && /\|?\s*-{2,}/.test(lines[i + 1]))) {
      flushParagraph(buf, out, `${prefix}p${i}`);
      const tableLines = [];
      while (i < lines.length && lines[i].includes("|")) {
        tableLines.push(lines[i]);
        i += 1;
      }
      if (isTable(tableLines)) out.push(table(tableLines, `${prefix}t${i}`));
      else buf.push(...tableLines);
      continue;
    }
    if (/^\s*[-*]\s+/.test(line)) {
      flushParagraph(buf, out, `${prefix}p${i}`);
      const items = [];
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*[-*]\s+/, ""));
        i += 1;
      }
      out.push(
        <ul key={`${prefix}ul${i}`}>
          {items.map((item, n) => {
            const checklist = item.match(/^\[([ xX])\]\s*(.*)$/);
            return (
              <li key={n} className={checklist ? "checklist-item" : undefined}>
                {checklist ? <input type="checkbox" checked={checklist[1].toLowerCase() === "x"} disabled aria-label={checklist[2] || "Checklist item"} /> : null}
                {inline(checklist ? checklist[2] : item)}
              </li>
            );
          })}
        </ul>,
      );
      continue;
    }
    if (/^\s*>/.test(line)) {
      flushParagraph(buf, out, `${prefix}p${i}`);
      const quote: string[] = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) {
        quote.push(lines[i].replace(/^\s*>\s?/, ""));
        i += 1;
      }
      out.push(<blockquote key={`${prefix}q${i}`}>{quote.map((entry, n) => <Fragment key={n}>{n ? <br /> : null}{inline(entry)}</Fragment>)}</blockquote>);
      continue;
    }
    if (/^\s*\d+\.\s+/.test(line)) {
      flushParagraph(buf, out, `${prefix}p${i}`);
      const items = [];
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*\d+\.\s+/, ""));
        i += 1;
      }
      out.push(
        <ol key={`${prefix}ol${i}`}>
          {items.map((item, n) => (
            <li key={n}>{inline(item)}</li>
          ))}
        </ol>,
      );
      continue;
    }
    if (!line.trim()) {
      flushParagraph(buf, out, `${prefix}p${i}`);
      i += 1;
      continue;
    }
    buf.push(line);
    i += 1;
  }
  flushParagraph(buf, out, `${prefix}end`);
  return out;
}

function blocks(text: string): ReactNode[] {
  const out: ReactNode[] = [];
  const parts = text.replace(/\r\n/g, "\n").split(/(```[\s\S]*?(?:```|$))/g);
  parts.forEach((part, idx) => {
    if (part.startsWith("```")) {
      const body = part.replace(/^```[a-zA-Z0-9_-]*\n?/, "").replace(/```$/, "");
      out.push(<CodeBlock key={`c${idx}`} body={body} />);
      return;
    }
    out.push(...renderSegment(part, `s${idx}`));
  });
  return out;
}

export function Markdown({ text }: { text: string }) {
  return <div className="prose">{blocks(text || "")}</div>;
}
