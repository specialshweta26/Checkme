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
    const [wy, wm, wd] = want.split('-').map(Number);
    const read = (() => {
  // A calendar on screen, read three ways (docs/BROWSING.md "Pickers"): cells with a full date label, month grids
  // (a "January 2027" heading and its day numbers), and the next / previous month arrows. Generic: no site names.
  const vis = (el) => { const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    return r.width > 2 && r.height > 2 && st.visibility !== 'hidden' && st.display !== 'none' && r.bottom > 0 && r.top < innerHeight; };
  const centre = (el) => { const r = el.getBoundingClientRect(); return { x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2) }; };
  const off = (el) => {
    const c = (String(el.className) + ' ' + String(el.parentElement && el.parentElement.className)).toLowerCase();
    return el.getAttribute('aria-disabled') === 'true' || el.hasAttribute('disabled') || /disabled|outside|other-?month|muted|past|blocked|unavailable/.test(c);
  };
  const cells = [];
  for (const el of document.querySelectorAll('[aria-label], [data-date], [datetime], [title], [role="gridcell"], td')) {
    if (!vis(el) || cells.length >= 400) continue;
    const text = [el.getAttribute('aria-label'), el.getAttribute('data-date'), el.getAttribute('datetime'), el.getAttribute('title')].filter(Boolean).join(' | ');
    if (!text) continue;
    cells.push({ text: text.slice(0, 120), ...centre(el), disabled: off(el) });
  }
  const MONTH = /^(jan(uary)?|feb(ruary)?|mar(ch)?|apr(il)?|may|june?|july?|aug(ust)?|sep(t(ember)?)?|oct(ober)?|nov(ember)?|dec(ember)?)\.?,?\s+'?(\d{2}|\d{4})$/i;
  const heads = [];
  for (const el of document.querySelectorAll('div, span, h1, h2, h3, h4, h5, h6, p, caption, th, strong, b, button, label')) {
    const t = (el.innerText || '').replace(/\s+/g, ' ').trim();
    if (t.length > 20 || !MONTH.test(t) || !vis(el)) continue;
    if (Array.from(el.children).some((c) => MONTH.test((c.innerText || '').replace(/\s+/g, ' ').trim()))) continue;  // the deepest one
    heads.push({ el, t });
  }
  const grids = [];
  for (const { el, t } of heads.slice(0, 4)) {
    let box = el.parentElement;
    for (let level = 0; box && level < 6; level++, box = box.parentElement) {
      const days = [];
      for (const n of box.querySelectorAll('*')) {
        if (n.children.length > 1) continue;
        const txt = (n.innerText || '').trim();
        if (!/^\d{1,2}$/.test(txt) || +txt < 1 || +txt > 31 || !vis(n)) continue;
        if (n.children.length === 1 && (n.children[0].innerText || '').trim() === txt) continue;  // keep the innermost
        days.push({ d: +txt, ...centre(n), disabled: off(n) });
      }
      if (days.length >= 28) {
        if (days.length <= 45) grids.push({ caption: t, days });
        break;
      }
    }
  }
  const arrow = (re) => {
    const el = Array.from(document.querySelectorAll('button, [role="button"], span, div, a, i, svg')).find((n) => {
      if (!vis(n) || n.getBoundingClientRect().width >= 80) return false;
      const s = [n.getAttribute('aria-label'), n.getAttribute('title'), n.getAttribute('data-testid'), n.className && String(n.className.baseVal || n.className)].join(' ').toLowerCase();
      return re.test(s);
    });
    return el ? centre(el) : null;
  };
  return {
    cells, grids,
    next: arrow(/next( month)?\b|navbutton--next|arrow-?right|chevron-?right|\bnext\b|right-?arrow/),
    prev: arrow(/prev(ious)?( month)?\b|navbutton--prev|arrow-?left|chevron-?left|\bprev\b|left-?arrow/),
  };
})();
    const cell = read.cells.find((c) => !c.disabled && c.text.split('|').some((p) => parseDate(p.trim()) === want));
    if (cell) return { ok: true, cell: { x: cell.x, y: cell.y } };
    const captionMonth = (t) => {
      const m = /^\s*([a-z]{3})[a-z]*\.?,?\s+'?(\d{2}|\d{4})\s*$/i.exec(t || '');
      if (!m || MONTHS.indexOf(m[1].toLowerCase()) < 0) return null;
      const y = +m[2];
      return [(y < 100 ? y + 2000 : y), MONTHS.indexOf(m[1].toLowerCase()) + 1];
    };
    const shown = [];
    for (const g of read.grids) {
      const ym = captionMonth(g.caption);
      if (!ym) continue;
      shown.push(ym[0] * 12 + ym[1]);
      if (ym[0] === wy && ym[1] === wm) {
        const day = g.days.find((d) => d.d === wd && !d.disabled);
        return day ? { ok: true, cell: { x: day.x, y: day.y } } : { ok: false };
      }
    }
    if (!shown.length) {
      for (const c of read.cells) for (const p of c.text.split('|')) {
        const iso = parseDate(p.trim());
        if (iso) shown.push(+iso.slice(0, 4) * 12 + +iso.slice(5, 7));
      }
    }
    if (!shown.length) return { ok: false };  // no calendar on screen: never click some other arrow (a carousel)
    const target = wy * 12 + wm;
    if (target > Math.max(...shown) && read.next) return { ok: true, next: read.next };
    if (target < Math.min(...shown) && read.prev) return { ok: true, next: read.prev };
    return { ok: false };
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
