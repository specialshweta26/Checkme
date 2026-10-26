"use client";

import { Fragment, type ReactNode, useMemo, useState } from "react";

function score(value: string) {
  const n = Number(String(value).replace(/[^0-9.+-]/g, ""));
  return Number.isFinite(n) && String(value).replace(/[₹,%\s,]/g, "") !== "" ? n : String(value).toLowerCase();
}

export function DataTable({
  head,
  body,
  cell,
}: {
  head: string[];
  body: string[][];
  cell: (text: string) => ReactNode;
}) {
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState({ col: 0, dir: 1 as 1 | -1 });
  const rows = useMemo(() => {
    const q = query.trim().toLowerCase();
    const filtered = q ? body.filter((row) => row.join(" ").toLowerCase().includes(q)) : body.slice();
    return filtered.sort((a, b) => {
      const av = score(a[sort.col] || "");
      const bv = score(b[sort.col] || "");
      if (av < bv) return -sort.dir;
      if (av > bv) return sort.dir;
      return 0;
    });
  }, [body, query, sort]);

  return (
    <div className="md-table-wrap">
      <div className="md-table-toolbar">
        <input
          type="search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Filter table…"
          aria-label="Filter table"
        />
        <span className="md-table-count">
          {rows.length} of {body.length}
        </span>
      </div>
      <table>
        <thead>
          <tr>
            {head.map((c, i) => (
              <th key={i}>
                <button
                  type="button"
                  className={`md-th-btn ${sort.col === i ? (sort.dir === 1 ? "asc" : "desc") : ""}`}
                  onClick={() => setSort((s) => ({ col: i, dir: s.col === i ? ((s.dir === 1 ? -1 : 1) as 1 | -1) : 1 }))}
                >
                  {cell(c)}
                </button>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, ri) => (
            <tr key={ri}>
              {row.map((c, ci) => (
                <td key={ci}>{cell(c)}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {!rows.length ? <p className="md-table-empty">No rows match this filter.</p> : null}
    </div>
  );
}

export function textNodes(text: string): ReactNode[] {
  const parts = text.split(/(\*\*[^*]+\*\*|\*[^*]+\*|`[^`]+`)/g);
  return parts.map((part, i) => {
    if (part.startsWith("**") && part.endsWith("**")) return <strong key={i}>{part.slice(2, -2)}</strong>;
    if (part.startsWith("*") && part.endsWith("*")) return <em key={i}>{part.slice(1, -1)}</em>;
    if (part.startsWith("`") && part.endsWith("`")) return <code key={i}>{part.slice(1, -1)}</code>;
    return <Fragment key={i}>{part}</Fragment>;
  });
}
