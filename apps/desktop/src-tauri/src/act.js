(function (ref, kind, text) {
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

  if (kind === 'click' || kind === 'check' || kind === 'uncheck') {
    target.click();
    return { ok: true };
  }
  if (kind === 'dblclick') {
    target.dispatchEvent(new MouseEvent('dblclick', { bubbles: true }));
    return { ok: true };
  }
  if (kind === 'hover') {
    target.dispatchEvent(new MouseEvent('mouseover', { bubbles: true }));
    return { ok: true };
  }
  if (kind === 'select' || kind === 'pick' || target.tagName === 'SELECT') {
    focusField(target);
    if (target.tagName === 'SELECT') {
      const want = String(text || '').toLowerCase();
      const opt = Array.from(target.options || []).find((o) => String(o.value || '').toLowerCase() === want || String(o.text || '').toLowerCase() === want);
      if (opt) target.value = opt.value;
      target.dispatchEvent(new Event('input', { bubbles: true }));
      target.dispatchEvent(new Event('change', { bubbles: true }));
    }
    return { ok: true, submit: kind === 'select' || kind === 'pick', composer: read(target) };
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
