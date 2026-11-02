(() => {
  const BASE = 'a, button, input, textarea, select, option, label, summary, [role="button"], [role="link"], [role="textbox"], [role="searchbox"], [role="checkbox"], [role="radio"], [role="combobox"], [role="option"], [role="listbox"], [role="tab"], [role="menuitem"], [contenteditable="true"]';
  const tags = {A: 'link', BUTTON: 'button', INPUT: 'textbox', TEXTAREA: 'textbox', SELECT: 'combobox', OPTION: 'option', LABEL: 'label', SUMMARY: 'button', LI: 'option'};
  const inputRole = {submit: 'button', button: 'button', reset: 'button', checkbox: 'checkbox', radio: 'radio', search: 'searchbox', file: 'file'};
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return false;
    const st = window.getComputedStyle(el);
    return st.visibility !== 'hidden' && st.display !== 'none' && Number(st.opacity || 1) > 0.05;
  };
  const inView = (el) => { const r = el.getBoundingClientRect(); return r.bottom > 0 && r.top < window.innerHeight; };
  document.querySelectorAll('[data-checkme-ref]').forEach((el) => el.removeAttribute('data-checkme-ref'));
  const seen = new Set();
  const ordered = [];
  for (const el of document.querySelectorAll(BASE)) {
    if (!el || seen.has(el) || !visible(el)) continue;
    seen.add(el);
    ordered.push(el);
  }
  const shown = ordered.filter(inView).concat(ordered.filter((el) => !inView(el))).slice(0, 150);
  const elements = shown.map((el, i) => {
    const ref = 'e' + i;
    try { el.setAttribute('data-checkme-ref', ref); } catch (e) {}
    const name = (el.getAttribute('aria-label') || el.innerText || el.getAttribute('placeholder') || el.value || el.getAttribute('title') || '').replace(/\s+/g, ' ').trim().slice(0, 80);
    const r = el.getBoundingClientRect();
    const role = (el.getAttribute('role') || (el.tagName === 'INPUT' && inputRole[(el.getAttribute('type') || '').toLowerCase()]) || tags[el.tagName] || el.tagName.toLowerCase());
    return { ref, role, name, tag: el.tagName.toLowerCase(), href: el.href || '', in_view: inView(el), x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2) };
  });
  const tree = elements.map((n) => ('- ' + n.ref + ' ' + n.role + ' ' + (n.name || '')).trim()).join('\n');
  return { url: location.href, title: document.title || '', text: (document.body && document.body.innerText || '').slice(0, 12000), tree, elements };
})()
