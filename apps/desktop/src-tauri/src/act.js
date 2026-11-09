(function (ref, kind, text, allowMouse) {
  // Same walk as snapshot.js: the page, open shadow roots and same-origin iframes.
  function find(root) {
    const hit = root.querySelector('[data-checkme-ref="' + ref + '"]');
    if (hit) return hit;
    for (const node of root.querySelectorAll('*')) {
      let inner = node.shadowRoot || null;
      if (!inner && (node.tagName === 'IFRAME' || node.tagName === 'FRAME')) {
        try { inner = node.contentDocument; } catch (e) {}
      }
      const found = inner && find(inner);
      if (found) return found;
    }
    return null;
  }
  const el = ref ? find(document) : null;
  if (!el && (kind !== 'press' || ref)) return { ok: false, not_found: true, error: 'ref not found: ' + ref };
  const target = el || document.activeElement;
  if (!target) return { ok: false, error: 'nothing to act on' };

  function read(node) {
    if (!node) return '';
    if (node.tagName === 'INPUT' || node.tagName === 'TEXTAREA') return String(node.value || '');
    if (node.isContentEditable) return String(node.innerText || '').replace(/\s+/g, ' ').trim();
    return '';
  }

  function focusField(node) {
    node.focus();
    if (node.tagName === 'INPUT' || node.tagName === 'TEXTAREA') {
      try { node.select(); } catch (e) {}
      return;
    }
    if (!node.isContentEditable) return;
    const sel = node.ownerDocument.getSelection();
    if (!sel) return;
    const range = node.ownerDocument.createRange();
    range.selectNodeContents(node);
    sel.removeAllRanges();
    sel.addRange(range);
  }

  // Whether b is a or inside it, across open shadow roots.
  function within(a, b) {
    for (let n = b; n; n = n.parentNode || n.host) if (n === a) return true;
    return false;
  }

  // Where a real mouse should go: the element's centre, scrolled into view, when nothing covers it. Only for the
  // main page (frame coordinates differ); null means "use the DOM event instead".
  function point(node) {
    if (!allowMouse || node.ownerDocument !== document) return null;
    try { node.scrollIntoView({ block: 'center', inline: 'center' }); } catch (e) {}
    const r = node.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return null;
    const x = r.left + r.width / 2;
    const y = r.top + r.height / 2;
    if (x < 0 || y < 0 || x > window.innerWidth || y > window.innerHeight) return null;
    let hit = document.elementFromPoint(x, y);
    while (hit && hit.shadowRoot) {
      const inner = hit.shadowRoot.elementFromPoint(x, y);
      if (!inner || inner === hit) break;
      hit = inner;
    }
    if (!hit || !(within(node, hit) || within(hit, node))) return null;
    return { x: Math.round(x), y: Math.round(y) };
  }

  const MONTHS = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'];
  const TIME = /T?(\d{1,2}):(\d{2})(?::\d{2})?\s*([ap]\.?m\.?)?/i;

  // A date from the usual ways it is written (ISO, 13/05/2026, 13 May 2026, May 13 2026); day-first when unclear.
  function parseDate(raw) {
    let t = String(raw || '').replace(/(\d)(st|nd|rd|th)\b/gi, '$1').replace(/,/g, ' ').replace(/\s+/g, ' ').trim();
    t = t.replace(TIME, '').trim();
    const mon = (name) => MONTHS.indexOf(String(name).slice(0, 3).toLowerCase()) + 1;
    let y = 0, m = 0, d = 0, hit;
    if ((hit = t.match(/^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$/))) { y = +hit[1]; m = +hit[2]; d = +hit[3]; }
    else if ((hit = t.match(/^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})$/))) {
      d = +hit[1]; m = +hit[2]; y = +hit[3];
      if (m > 12 && d <= 12) { const swap = d; d = m; m = swap; }
    }
    else if ((hit = t.match(/^(?:[a-z]+ )?(\d{1,2}) ([a-z]{3,}) (\d{4})$/i))) { d = +hit[1]; m = mon(hit[2]); y = +hit[3]; }
    else if ((hit = t.match(/^(?:[a-z]+ )?([a-z]{3,}) (\d{1,2}) (\d{4})$/i))) { m = mon(hit[1]); d = +hit[2]; y = +hit[3]; }
    else if ((hit = t.match(/^([a-z]{3,}) (\d{4})$/i))) { m = mon(hit[1]); d = 1; y = +hit[2]; }
    else if ((hit = t.match(/^(\d{4})-(\d{2})$/))) { y = +hit[1]; m = +hit[2]; d = 1; }
    if (!y || m < 1 || m > 12 || d < 1 || d > 31) return null;
    const day = new Date(Date.UTC(y, m - 1, d));
    if (day.getUTCDate() !== d) return null;
    return day.toISOString().slice(0, 10);
  }

  function parseTime(raw) {
    const hit = String(raw || '').match(TIME);
    if (!hit) return '';
    let h = +hit[1];
    const half = (hit[3] || '').toLowerCase().replace(/\./g, '');
    if (half === 'pm' && h < 12) h += 12;
    if (half === 'am' && h === 12) h = 0;
    if (h > 23 || +hit[2] > 59) return '';
    return String(h).padStart(2, '0') + ':' + hit[2];
  }

  // The ISO value a native date / time input accepts, or null.
  function nativeDate(raw, type) {
    if (type === 'time') return parseTime(raw) || null;
    if (type === 'week') return /^\d{4}-W\d{2}$/.test(String(raw).trim()) ? String(raw).trim() : null;
    const day = parseDate(raw);
    if (!day) return null;
    if (type === 'month') return day.slice(0, 7);
    if (type === 'datetime-local') return day + 'T' + (parseTime(raw) || '00:00');
    return day;
  }

  if (kind === 'fill' && target.tagName === 'INPUT') {
    const type = (target.getAttribute('type') || '').toLowerCase();
    if (['date', 'datetime-local', 'month', 'time', 'week'].includes(type)) {
      const value = nativeDate(text, type);
      if (!value) return { ok: false, error: 'could not read "' + text + '" as a date for this date field; give it as YYYY-MM-DD' };
      const win = target.ownerDocument.defaultView || window;
      const setter = Object.getOwnPropertyDescriptor(win.HTMLInputElement.prototype, 'value').set;
      target.focus();
      setter.call(target, value);
      target.dispatchEvent(new Event('input', { bubbles: true }));
      target.dispatchEvent(new Event('change', { bubbles: true }));
      return { ok: true, composer: read(target) };
    }
  }

  // A box that shows a value but takes no typing (a read-only input, a label, a div) opens a picker when clicked:
  // mark the boxes already on the page, then let the app click it and fill what opens (picker.js).
  const typableField = (el) => el.isContentEditable || ((el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') && !el.readOnly && !el.disabled
    && !['button', 'submit', 'checkbox', 'radio', 'hidden', 'file', 'image', 'reset'].includes((el.getAttribute('type') || '').toLowerCase()));
  if (kind === 'pick_date' || kind === 'date') {
    const at = point(target);
    if (!at) target.click();
    return { ok: true, picker: true, dateOnly: true, mouse: at || undefined, clicks: 1 };
  }
  if ((kind === 'fill' || kind === 'type') && !typableField(target)) {
    for (const el of document.querySelectorAll('input, textarea, [contenteditable="true"]')) {
      const r = el.getBoundingClientRect();
      if (r.width > 4 && r.height > 4) el.setAttribute('data-checkme-seen', '1');
    }
    const at = point(target);
    if (!at) target.click();
    return { ok: true, picker: true, mouse: at || undefined, clicks: 1 };
  }

  // The page's own click, for when a real mouse press had no effect. Refused on final controls: a second click there
  // could repeat an order.
  if (kind === 'dom_click') {
    const label = String(target.getAttribute('aria-label') || target.innerText || target.value || '');
    if (/\b(pay|buy|purchase|order|checkout|finish|complete|confirm|submit|send|transfer|delete|remove account|book)\b/i.test(label)) return { ok: true, skipped: true };
    target.click();
    return { ok: true };
  }
  if (kind === 'click' || kind === 'dblclick') {
    // A real mouse press opens menus and widgets that ignore a synthetic click (pointerdown / mousedown handlers).
    const at = point(target);
    if (at) return { ok: true, mouse: at, clicks: kind === 'dblclick' ? 2 : 1 };
    if (kind === 'dblclick') {
      target.dispatchEvent(new MouseEvent('dblclick', { bubbles: true }));
      return { ok: true };
    }
    target.click();
    return { ok: true };
  }
  if (kind === 'rightclick' || kind === 'contextclick' || kind === 'context_click') {
    const at = point(target);
    if (at) return { ok: true, mouse: at, clicks: 1, button: 'right' };
    target.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true }));
    return { ok: true };
  }
  if (kind === 'check' || kind === 'uncheck') {
    target.click();
    return { ok: true };
  }
  if (kind === 'hover') {
    // Menus that open on hover listen for real pointer movement (and CSS :hover needs it), so move the mouse there.
    const at = point(target);
    if (at) return { ok: true, mouse: at, clicks: 0 };
    for (const type of ['pointerover', 'pointerenter', 'mouseover', 'mouseenter', 'pointermove', 'mousemove']) {
      const Ev = type.startsWith('pointer') && window.PointerEvent ? PointerEvent : MouseEvent;
      target.dispatchEvent(new Ev(type, { bubbles: !type.endsWith('enter') }));
    }
    return { ok: true };
  }
  if (target.tagName === 'SELECT' && ['select', 'pick', 'fill', 'type'].includes(kind)) {
    const want = String(text || '').replace(/\s+/g, ' ').trim().toLowerCase();
    const options = Array.from(target.options || []);
    const label = (o) => String(o.text || '').replace(/\s+/g, ' ').trim();
    const opt = !want ? null : options.find((o) => String(o.value || '').toLowerCase() === want || label(o).toLowerCase() === want)
      || options.find((o) => label(o).toLowerCase().includes(want));
    if (!opt) return { ok: false, error: 'this dropdown has no option "' + text + '". Its options are: ' + options.map(label).join(' | ') };
    // Already on it: nothing will change on the page, and that isn't a failure.
    if (target.value === opt.value) return { ok: true, selected: label(opt), already: true, composer: read(target) };
    focusField(target);
    target.value = opt.value;
    target.dispatchEvent(new Event('input', { bubbles: true }));
    target.dispatchEvent(new Event('change', { bubbles: true }));
    return { ok: true, selected: label(opt), composer: read(target) };
  }
  if (kind === 'select' || kind === 'pick') {
    // Pressing Enter on a link would follow it, so a link is never treated as a dropdown.
    if (target.tagName === 'A' && target.getAttribute('href') && target.getAttribute('role') !== 'combobox') {
      return { ok: false, error: 'this is a link, not a dropdown: click it to follow it, or take a snapshot and use the dropdown\'s own ref' };
    }
    // A custom dropdown: open it, type the value and choose the match (picker.js).
    for (const el of document.querySelectorAll('input, textarea, [contenteditable="true"]')) {
      const r = el.getBoundingClientRect();
      if (el !== target && r.width > 4 && r.height > 4) el.setAttribute('data-checkme-seen', '1');
    }
    const at = point(target);
    if (!at) target.click();
    return { ok: true, picker: !!text, mouse: at || undefined, clicks: 1 };
  }
  if (kind === 'fill' || kind === 'type') {
    focusField(target);
    return { ok: true, insert: true, submit: kind === 'type', composer: read(target) };
  }
  if (kind === 'press') {
    focusField(target);
    return { ok: true, submit: true, key: text || 'Enter' };
  }
  target.click();
  return { ok: true };
})
