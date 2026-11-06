(function (mode, text) {
  // Click-to-open pickers (docs/BROWSING.md "Pickers"): a box that shows a value but takes no typing opens a search
  // box with suggestions, or a calendar, when clicked. The app clicks with a real mouse between these calls.
  // mode: "mark" (before the click), "focus" (find the box to type in), "calendar" (find the day or the next-month
  // arrow), "suggest" (find the best suggestion for text). Generic: no site names.
  const vis = (el) => {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    return r.width > 2 && r.height > 2 && st.visibility !== 'hidden' && st.display !== 'none' && r.bottom > 0 && r.top < innerHeight;
  };
  const typable = (el) => el && (el.isContentEditable || ((el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') && !el.readOnly && !el.disabled
    && !['button', 'submit', 'checkbox', 'radio', 'hidden', 'file', 'image', 'reset'].includes((el.getAttribute('type') || '').toLowerCase())));
  const centre = (el) => { const r = el.getBoundingClientRect(); return { x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2) }; };

  if (mode === 'mark') {
    for (const el of document.querySelectorAll('input, textarea, [contenteditable="true"]')) if (vis(el)) el.setAttribute('data-checkme-seen', '1');
    return { ok: true };
  }

  if (mode === 'focus') {
    let el = document.activeElement;
    if (!typable(el) || !vis(el)) {
      el = Array.from(document.querySelectorAll('input, textarea, [contenteditable="true"]'))
        .find((n) => !n.hasAttribute('data-checkme-seen') && typable(n) && vis(n)) || null;
      if (el) el.focus();
    }
    document.querySelectorAll('[data-checkme-seen]').forEach((n) => n.removeAttribute('data-checkme-seen'));
    if (el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA')) {
      try { el.select(); } catch (e) {}
    }
    return { ok: !!el };
  }

  const MONTHS = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'];
  function parseDate(raw) {
    let t = String(raw || '').replace(/(\d)(st|nd|rd|th)\b/gi, '$1').replace(/,/g, ' ').replace(/\s+/g, ' ').trim();
    t = t.replace(/T?(\d{1,2}):(\d{2})(?::\d{2})?\s*([ap]\.?m\.?)?/i, '').trim();
    const mon = (name) => MONTHS.indexOf(String(name).slice(0, 3).toLowerCase()) + 1;
    let y = 0, m = 0, d = 0, hit;
    if ((hit = t.match(/^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$/))) { y = +hit[1]; m = +hit[2]; d = +hit[3]; }
    else if ((hit = t.match(/^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})$/))) { d = +hit[1]; m = +hit[2]; y = +hit[3]; if (m > 12 && d <= 12) { const s = d; d = m; m = s; } }
    else if ((hit = t.match(/^(?:[a-z]+ )?(\d{1,2}) ([a-z]{3,}) (\d{4})$/i))) { d = +hit[1]; m = mon(hit[2]); y = +hit[3]; }
    else if ((hit = t.match(/^(?:[a-z]+ )?([a-z]{3,}) (\d{1,2}) (\d{4})$/i))) { m = mon(hit[1]); d = +hit[2]; y = +hit[3]; }
    if (!y || m < 1 || m > 12 || d < 1 || d > 31) return '';
    return y + '-' + String(m).padStart(2, '0') + '-' + String(d).padStart(2, '0');
  }

  if (mode === 'calendar') {
    const want = parseDate(text);
    if (!want) return { ok: false };
    let dated = 0;
    for (const el of document.querySelectorAll('[aria-label], [data-date], [datetime], [title], [role="gridcell"], td')) {
      if (!vis(el)) continue;
      if (el.getAttribute('aria-disabled') === 'true' || String(el.className).toLowerCase().includes('disabled')) continue;
      const parts = [el.getAttribute('aria-label'), el.getAttribute('data-date'), el.getAttribute('datetime'), el.getAttribute('title')].filter(Boolean);
      if (parts.some((p) => parseDate(p))) dated += 1;
      if (parts.some((p) => parseDate(p) === want)) return { ok: true, cell: centre(el) };
    }
    if (!dated) return { ok: false };  // no calendar on screen: never click some other "next" arrow (a carousel)
    const next = Array.from(document.querySelectorAll('button, [role="button"], span, div, a')).find((el) => {
      if (!vis(el) || el.getBoundingClientRect().width >= 80) return false;
      const t = [el.getAttribute('aria-label'), el.getAttribute('title'), el.className && String(el.className)].join(' ').toLowerCase();
      return /next( month)?\b|navbutton--next|arrow-?right|chevron-?right|\bnext\b/.test(t);
    });
    return next ? { ok: true, next: centre(next) } : { ok: false };
  }

  if (mode === 'suggest') {
    const q = String(text || '').trim().toLowerCase();
    const sel = '[role="option"], [role="menuitem"], [role="listbox"] li, [role="listbox"] button, [role="menu"] li, ul li, [role="dialog"] li';
    let best = null;
    let bestScore = 0;
    for (const el of document.querySelectorAll(sel)) {
      if (!vis(el)) continue;
      const name = (el.getAttribute('aria-label') || el.innerText || '').replace(/\s+/g, ' ').trim().toLowerCase();
      if (!name || name.length > 200) continue;
      const score = name === q ? 100 : name.startsWith(q) ? 80 : name.includes(q) ? 60 : 0;
      if (score > bestScore) { best = el; bestScore = score; }
    }
    return best ? { ok: true, at: centre(best) } : { ok: false };
  }
  return { ok: false };
})
