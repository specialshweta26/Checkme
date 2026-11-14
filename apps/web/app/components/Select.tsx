"use client";

import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { AgentAvatar } from "./AgentAvatar";
import { loadWorkspace, type WorkspaceAgent } from "../lib";

// One dropdown for the whole app (docs/DESIGN.md "Dropdowns"). Native <select> menus are drawn by the OS (grey on
// iOS and macOS, whatever the theme), so every picker uses this instead: themed in light and dark, a bottom sheet on
// phones, full keyboard support (arrows, Home/End, Enter, Escape, type a letter to jump), and an icon per option.

export type SelectOption = { value: string; label: string; icon?: ReactNode; hint?: string };

type Props = {
  value: string;
  options: SelectOption[];
  onChange: (value: string) => void;
  label: string; // accessible name (and the sheet's title on phones)
  className?: string;
  variant?: "field" | "bare"; // bare: no border, for composite controls like the Today composer
  disabled?: boolean;
  placeholder?: string;
};

const PHONE = 640;

export function Select({ value, options, onChange, label, className = "", variant = "field", disabled, placeholder = "Choose…" }: Props) {
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const [pos, setPos] = useState<{ left: number; top?: number; bottom?: number; width: number; maxHeight: number } | null>(null);
  const [sheet, setSheet] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const list = useRef<HTMLUListElement>(null);
  const typed = useRef({ text: "", at: 0 });
  const id = useId();
  const current = options.find((o) => o.value === value);

  function place() {
    const box = trigger.current?.getBoundingClientRect();
    if (!box) return;
    const phone = window.innerWidth < PHONE;
    setSheet(phone);
    if (phone) return;
    const width = Math.min(Math.max(box.width, 220), window.innerWidth - 16);
    const left = Math.min(Math.max(8, box.left), window.innerWidth - width - 8);
    const below = window.innerHeight - box.bottom - 12;
    const above = box.top - 12;
    if (below >= Math.min(320, options.length * 40 + 12) || below >= above) {
      setPos({ left, top: box.bottom + 6, width, maxHeight: Math.min(360, below) });
    } else {
      setPos({ left, bottom: window.innerHeight - box.top + 6, width, maxHeight: Math.min(360, above) });
    }
  }

  function openList() {
    if (disabled || !options.length) return;
    setActive(Math.max(0, options.findIndex((o) => o.value === value)));
    place();
    setOpen(true);
  }

  function close(refocus = true) {
    setOpen(false);
    if (refocus) trigger.current?.focus();
  }

  function choose(i: number) {
    const o = options[i];
    if (!o) return;
    if (o.value !== value) onChange(o.value);
    close();
  }

  useLayoutEffect(() => {
    if (!open) return;
    list.current?.focus();
    list.current?.querySelector<HTMLElement>(`[data-i="${active}"]`)?.scrollIntoView({ block: "nearest" });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  useEffect(() => {
    if (!open) return;
    list.current?.querySelector<HTMLElement>(`[data-i="${active}"]`)?.scrollIntoView({ block: "nearest" });
  }, [active, open]);

  useEffect(() => {
    if (!open) return;
    const away = (e: Event) => {
      const t = e.target as Node;
      if (!list.current?.contains(t) && !trigger.current?.contains(t)) close(false);
    };
    const reflow = () => place();
    document.addEventListener("pointerdown", away, true);
    window.addEventListener("resize", reflow);
    window.addEventListener("scroll", reflow, true);
    return () => {
      document.removeEventListener("pointerdown", away, true);
      window.removeEventListener("resize", reflow);
      window.removeEventListener("scroll", reflow, true);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  function onListKey(e: React.KeyboardEvent) {
    const last = options.length - 1;
    if (e.key === "ArrowDown") setActive((i) => Math.min(last, i + 1));
    else if (e.key === "ArrowUp") setActive((i) => Math.max(0, i - 1));
    else if (e.key === "Home") setActive(0);
    else if (e.key === "End") setActive(last);
    else if (e.key === "Enter" || e.key === " ") choose(active);
    else if (e.key === "Escape" || e.key === "Tab") return close(e.key === "Escape");
    else if (e.key.length === 1 && /\S/.test(e.key)) {
      const now = Date.now();
      typed.current = { text: (now - typed.current.at < 700 ? typed.current.text : "") + e.key.toLowerCase(), at: now };
      const hit = options.findIndex((o) => o.label.toLowerCase().startsWith(typed.current.text));
      if (hit >= 0) setActive(hit);
    } else return;
    e.preventDefault();
  }

  const menu = open ? (
    <>
      {sheet ? <div className="select-scrim" onClick={() => close(false)} /> : null}
      <div
        className={`select-menu${sheet ? " select-sheet" : ""}`}
        style={sheet || !pos ? undefined : { left: pos.left, top: pos.top, bottom: pos.bottom, width: pos.width, maxHeight: pos.maxHeight }}
      >
        {sheet ? <div className="select-sheet-title">{label}</div> : null}
        <ul ref={list} id={`${id}-list`} role="listbox" aria-label={label} tabIndex={-1} aria-activedescendant={`${id}-o${active}`} onKeyDown={onListKey}>
          {options.map((o, i) => (
            <li
              key={o.value}
              id={`${id}-o${i}`}
              data-i={i}
              role="option"
              aria-selected={o.value === value}
              className={`${i === active ? "active" : ""}${o.value === value ? " chosen" : ""}`}
              onPointerMove={() => setActive(i)}
              onClick={() => choose(i)}
            >
              {o.icon ? <span className="select-icon">{o.icon}</span> : null}
              <span className="select-text">
                <span>{o.label}</span>
                {o.hint ? <small>{o.hint}</small> : null}
              </span>
              <svg className="select-check" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
                <path d="m5 12.5 4.5 4.5L19 7" />
              </svg>
            </li>
          ))}
        </ul>
      </div>
    </>
  ) : null;

  return (
    <>
      <button
        ref={trigger}
        type="button"
        className={`select-trigger ${variant}${open ? " open" : ""} ${className}`}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? `${id}-list` : undefined}
        aria-label={`${label}: ${current?.label || placeholder}`}
        disabled={disabled}
        onClick={() => (open ? close() : openList())}
        onKeyDown={(e) => {
          if (["ArrowDown", "ArrowUp", "Enter", " "].includes(e.key)) {
            e.preventDefault();
            openList();
          }
        }}
      >
        {current?.icon ? <span className="select-icon">{current.icon}</span> : null}
        <span className="select-value">{current?.label || placeholder}</span>
        <svg className="select-chevron" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
          <path d="m6 9 6 6 6-6" />
        </svg>
      </button>
      {menu && typeof document !== "undefined" ? createPortal(menu, document.body) : null}
    </>
  );
}

/** Agents in the sidebar's order, each with its own avatar. ``only`` limits the list; ``allLabel`` adds an
 * "all agents" choice with the value "". */
export function AgentSelect({
  value,
  onChange,
  label = "Agent",
  only,
  allLabel,
  variant,
  className,
  agents: given,
}: {
  value: string;
  onChange: (id: string) => void;
  label?: string;
  only?: string[];
  allLabel?: string;
  variant?: "field" | "bare";
  className?: string;
  agents?: WorkspaceAgent[];
}) {
  const [roster, setRoster] = useState<WorkspaceAgent[]>(given || []);
  useEffect(() => {
    if (given) {
      setRoster(given);
      return;
    }
    loadWorkspace()
      .then((w) => setRoster((w.agents || []).filter((a) => !a.archived)))
      .catch(() => undefined);
  }, [given]);
  const options = useMemo(() => {
    const rows = only ? only.map((id) => roster.find((a) => a.id === id)).filter((a): a is WorkspaceAgent => Boolean(a)) : roster;
    const out: SelectOption[] = rows.map((a) => ({
      value: a.id,
      label: a.name,
      hint: a.title || undefined,
      icon: <AgentAvatar name={a.name} color={a.color} avatar={a.avatar} className="agent-avatar tiny" />,
    }));
    if (allLabel) {
      out.unshift({
        value: "",
        label: allLabel,
        icon: (
          <span className="select-all-icon" aria-hidden>
            {rows.slice(0, 3).map((a) => (
              <AgentAvatar key={a.id} name={a.name} color={a.color} avatar={a.avatar} className="agent-avatar tiny" />
            ))}
          </span>
        ),
      });
    }
    return out;
  }, [roster, only, allLabel]);
  return <Select value={value} options={options} onChange={onChange} label={label} variant={variant} className={className} />;
}
