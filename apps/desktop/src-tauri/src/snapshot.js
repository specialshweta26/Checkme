(function (start, limit, find) {
  const BASE = 'a, button, input, textarea, select, option, label, summary, [role="button"], [role="link"], [role="textbox"], [role="searchbox"], [role="checkbox"], [role="radio"], [role="combobox"], [role="option"], [role="listbox"], [role="tab"], [role="menuitem"], [contenteditable="true"], [role="gridcell"], [data-date], td[title], [aria-label][tabindex], [draggable="true"]';
  const tags = {A: 'link', BUTTON: 'button', INPUT: 'textbox', TEXTAREA: 'textbox', SELECT: 'combobox', OPTION: 'option', LABEL: 'label', SUMMARY: 'button', LI: 'option', TH: 'columnheader'};
  const inputRole = {submit: 'button', button: 'button', reset: 'button', checkbox: 'checkbox', radio: 'radio', search: 'searchbox', file: 'file'};
  // Walks the page, open shadow roots and same-origin iframes. Other-origin frames are read separately (browser.rs).
  const roots = [];
  const addRoots = (root, ox, oy) => {
    roots.push({ root, ox, oy });
    for (const el of root.querySelectorAll('*')) {
      if (el.shadowRoot) addRoots(el.shadowRoot, ox, oy);
      if (el.tagName === 'IFRAME' || el.tagName === 'FRAME') {
        let doc = null;
        try { doc = el.contentDocument; } catch (e) {}
        if (doc) {
          const r = el.getBoundingClientRect();
          addRoots(doc, ox + r.x, oy + r.y);
        }
      }
    }
  };
  addRoots(document, 0, 0);
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return false;
    // A closed side menu is parked off the page or marked hidden: its links aren't there for a person to click.
    if (r.right <= 0 || el.closest('[aria-hidden="true"]')) return false;
    const st = window.getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none') return false;
    if (Number(st.opacity || 1) > 0.05) return true;
    // Styled dropdowns, tick boxes and file pickers keep the real control see-through on top of their artwork.
    const type = (el.getAttribute('type') || '').toLowerCase();
    return el.tagName === 'SELECT' || (el.tagName === 'INPUT' && ['checkbox', 'radio', 'file'].includes(type));
  };
  const inView = (item) => { const r = item.el.getBoundingClientRect(); return r.bottom + item.oy > 0 && r.top + item.oy < window.innerHeight; };
  // Values of these fields never leave the page.
  const isSecret = (el) => {
    const type = (el.getAttribute('type') || '').toLowerCase();
    const auto = (el.getAttribute('autocomplete') || '').toLowerCase();
    return type === 'password' || auto.startsWith('cc-') || auto === 'one-time-code';
  };
  const INTERACTIVE = 'a, button, input, textarea, select, option, label, summary, [role="button"], [role="link"], [role="checkbox"], [role="radio"], [role="tab"], [role="menuitem"], [role="option"], [contenteditable="true"]';
  // Clickable things that aren't links or buttons: table headers that sort, and anything the page styles as clickable.
  const looksClickable = (root) => {
    const out = [];
    const nodes = Array.from(root.querySelectorAll('th, [onclick], [oncontextmenu], [ondblclick], [onmouseover], [onmouseenter], figure, div, span, li, td, img, svg')).slice(0, 2500);
    for (const el of nodes) {
      if (el.closest(INTERACTIVE)) continue;
      if (el.tagName === 'TH' || el.tagName === 'FIGURE' || ['onclick', 'oncontextmenu', 'ondblclick', 'onmouseover', 'onmouseenter'].some((a) => el.hasAttribute(a))) { out.push(el); continue; }
      if (el.tagName === 'IMG' && el.getBoundingClientRect().width >= 40 && !el.closest('figure')) { out.push(el); continue; }
      const st = window.getComputedStyle(el);
      if (st.cursor !== 'pointer') continue;
      const up = el.parentElement;
      if (up && window.getComputedStyle(up).cursor === 'pointer') continue;
      out.push(el);
    }
    return out.slice(0, 80);
  };
  const seen = new Set();
  const ordered = [];
  for (const { root, ox, oy } of roots) {
    root.querySelectorAll('[data-checkme-ref]').forEach((el) => el.removeAttribute('data-checkme-ref'));
    for (const el of [...root.querySelectorAll(BASE), ...looksClickable(root)]) {
      if (!el || seen.has(el) || !visible(el)) continue;
      seen.add(el);
      ordered.push({ el, ox, oy });
    }
  }
  // What a control is showing right now, so a step can be checked without guessing from the page's text.
  const stateOf = (el) => {
    const out = {};
    const type = (el.getAttribute('type') || '').toLowerCase();
    const aria = (name) => el.getAttribute('aria-' + name);
    if (el.tagName === 'INPUT' && (type === 'checkbox' || type === 'radio')) out.checked = !!el.checked;
    else if (aria('checked') !== null) out.checked = aria('checked') === 'true';
    if (aria('selected') !== null) out.selected = aria('selected') === 'true';
    if (aria('expanded') !== null) out.expanded = aria('expanded') === 'true';
    if (el.disabled === true || aria('disabled') === 'true') out.disabled = true;
    const secret = type === 'password' || (el.getAttribute('autocomplete') || '').toLowerCase().startsWith('cc-') || (el.getAttribute('autocomplete') || '').toLowerCase() === 'one-time-code';
    if ((el.tagName === 'TEXTAREA' || (el.tagName === 'INPUT' && !['checkbox', 'radio', 'button', 'submit', 'reset', 'file', 'image', 'hidden'].includes(type))) && !secret) {
      out.value = String(el.value || '').slice(0, 80);
    }
    return out;
  };
  // A tick box's value is usually just "on": its label, or the text beside it, says what it is.
  const boxName = (el) => {
    const labelled = el.labels && el.labels[0] ? el.labels[0].innerText : '';
    const beside = el.nextSibling && el.nextSibling.nodeType === 3 ? el.nextSibling.textContent : (el.previousSibling && el.previousSibling.nodeType === 3 ? el.previousSibling.textContent : '');
    return (el.getAttribute('aria-label') || labelled || beside || el.getAttribute('name') || '').replace(/\s+/g, ' ').trim().slice(0, 80);
  };
  const isBox = (el) => el.tagName === 'INPUT' && ['checkbox', 'radio'].includes((el.getAttribute('type') || '').toLowerCase());
  const first = Number(start) || 0;
  // find: words the agent is looking for; matching elements go first so a long page doesn't hide them.
  const words = String(find || '').toLowerCase().split(/\s+/).filter((w) => w.length > 1);
  const hay = (el) => [el.getAttribute('aria-label'), el.innerText, el.getAttribute('placeholder'), el.getAttribute('title'), el.getAttribute('name'), el.id]
    .filter(Boolean).join(' ').toLowerCase();
  // Page order keeps refs the same after a scroll; on-screen first only matters when the list has to be cut.
  const byView = ordered.length <= (Number(limit) || 150) ? ordered : ordered.filter(inView).concat(ordered.filter((item) => !inView(item)));
  const ranked = words.length
    ? byView.map((item, i) => [item, words.reduce((n, w) => n + (hay(item.el).includes(w) ? 1 : 0), 0), i])
        .sort((a, b) => (b[1] - a[1]) || (a[2] - b[2])).map((row) => row[0])
    : byView;
  // A field with no name of its own: its label through aria, or the short text of the box around it ("Select State").
  const contextName = (el) => {
    if (!['INPUT', 'TEXTAREA'].includes(el.tagName) && el.getAttribute('role') !== 'combobox') {
      return String(el.getAttribute('alt') || el.getAttribute('title') || el.id || '').slice(0, 80);
    }
    const by = (el.getAttribute('aria-labelledby') || '').split(/\s+/).map((id) => { const n = id && el.ownerDocument.getElementById(id); return n ? n.textContent : ''; }).join(' ').trim();
    if (by) return by.slice(0, 80);
    let box = el.parentElement;
    for (let depth = 0; box && depth < 4; depth += 1, box = box.parentElement) {
      const lines = String(box.innerText || '').split('\n').map((l) => l.trim()).filter(Boolean);
      // Text beside another control belongs to that control, not to this field.
      if (lines.length > 2 || box.querySelector('button, a[href], select, [role="button"]')) return '';
      if (lines.length) return lines.join(' ').slice(0, 80);
    }
    return '';
  };
  // Identical controls ("Add to cart" on every product) say what they belong to.
  const nearText = (el, name) => {
    if (el.tagName === 'TH' && el.closest('table')) {
      const table = el.closest('table');
      const label = table.getAttribute('aria-label') || (table.caption && table.caption.innerText) || table.id;
      return String(label || 'table ' + (Array.from(el.ownerDocument.querySelectorAll('table')).indexOf(table) + 1)).slice(0, 50);
    }
    let best = '';
    let node = el.parentElement;
    for (let depth = 0; node && depth < 6; depth += 1, node = node.parentElement) {
      const lines = String(node.innerText || '').split('\n').map((l) => l.trim()).filter(Boolean);
      if (lines.filter((l) => l === name).length > 1) break;
      const others = lines.filter((l) => l !== name && l.length > 2);
      if (others.length) best = others[0].replace(/\s+/g, ' ').slice(0, 50);
      if (others.length >= 2) break;
    }
    return best;
  };
  const addNear = (rows, elOf) => {
    const counts = {};
    // A label and the box it names (a checkbox and "Sports") share a name without being two of the same control, so the
    // label doesn't count, and neither gets a neighbour's text in brackets ("Sports (Reading)" read as Reading).
    const labelOfListed = (el) => !!(el && el.tagName === 'LABEL' && el.control);
    rows.forEach((n, i) => { if (n.name && !labelOfListed(elOf(i))) counts[n.name] = (counts[n.name] || 0) + 1; });
    rows.forEach((n, i) => { if (!n.name && !n.options) { n.name = contextName(elOf(i)); } });
    let budget = 60;
    rows.forEach((n, i) => {
      if (counts[n.name] > 1 && budget > 0 && !labelOfListed(elOf(i))) {
        budget -= 1;
        const near = nearText(elOf(i), n.name);
        if (near && !n.name.includes(near)) n.near = near;
      }
    });
    return rows;
  };
  const shown = ranked.slice(0, Number(limit) || 150);
  const elements = shown.map((item, i) => {
    const el = item.el;
    const ref = 'e' + (first + i);
    try { el.setAttribute('data-checkme-ref', ref); } catch (e) {}
    const name = (el.getAttribute('aria-label') || el.innerText || el.getAttribute('placeholder') || (isSecret(el) ? '' : el.value) || el.getAttribute('title') || '').replace(/\s+/g, ' ').trim().slice(0, 80);
    const r = el.getBoundingClientRect();
    const role = (el.getAttribute('role') || (el.tagName === 'INPUT' && inputRole[(el.getAttribute('type') || '').toLowerCase()]) || tags[el.tagName] || el.tagName.toLowerCase());
    const choices = el.tagName === 'SELECT' ? Array.from(el.options || []).slice(0, 25).map((o) => (o.text || '').replace(/\s+/g, ' ').trim().slice(0, 40)) : null;
    const chosen = el.tagName === 'SELECT' && el.selectedOptions && el.selectedOptions[0] ? (el.selectedOptions[0].text || '').trim().slice(0, 40) : '';
    return { ref, role, ...stateOf(el), name: isBox(el) ? boxName(el) : el.tagName === 'SELECT' ? (el.getAttribute('aria-label') || (el.labels && el.labels[0] && el.labels[0].innerText) || el.getAttribute('name') || '').replace(/\s+/g, ' ').trim().slice(0, 80) : name, tag: el.tagName.toLowerCase(), href: el.href || '', in_view: inView(item), x: Math.round(r.x + r.width / 2 + item.ox), y: Math.round(r.y + r.height / 2 + item.oy), ...(choices ? { options: choices, selected: chosen } : {}) };
  });
  addNear(elements, (i) => shown[i].el);
  const tree = elements.map((n) => ('- ' + n.ref + ' ' + n.role + ' ' + (n.name || '') + (n.near ? ' (' + n.near + ')' : '') + (n.href ? ' ' + n.href : '') + (n.options ? ' [dropdown, now: ' + n.selected + '; options: ' + n.options.join(' | ') + ']' : '') + (n.checked === undefined ? '' : (n.checked ? ' [checked]' : ' [not checked]')) + (n.selected === true ? ' [selected]' : '') + (n.expanded === undefined ? '' : (n.expanded ? ' [expanded]' : ' [collapsed]')) + (n.disabled ? ' [disabled]' : '') + (n.value ? ' [value: ' + n.value + ']' : '')).trim()).join('\n');
  const parts = [];
  for (const { root } of roots) {
    const body = root.body;
    const piece = body ? body.innerText : Array.from(root.children || []).map((c) => c.innerText || '').join('\n');
    if (piece && piece.trim()) parts.push(piece);
  }
  const full = parts.join('\n').replace(/\n{3,}/g, '\n\n');
  const omitted = full.length > 10000;
  const head = omitted ? full.slice(0, 5000) : full;
  const tail = omitted ? full.slice(-5000) : '';
  let active = document.activeElement;
  while (active) {
    let inner = null;
    try { inner = active.shadowRoot ? active.shadowRoot.activeElement : (active.contentDocument && active.contentDocument.activeElement); } catch (e) {}
    if (!inner) break;
    active = inner;
  }
  let composer = '';
  if (active && !isSecret(active)) {
    if (active.tagName === 'INPUT' || active.tagName === 'TEXTAREA') composer = String(active.value || '');
    else if (active.isContentEditable) composer = String(active.innerText || '').replace(/\s+/g, ' ').trim();
  }
  return {
    url: location.href,
    title: document.title || '',
    text: tail ? head + '\n…\n' + tail : head,
    text_omitted: omitted,
    composer: composer.slice(0, 500),
    tree,
    elements,
    total: ordered.length
  };
})
