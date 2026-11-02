(function (ref, kind, text) {
  const el = document.querySelector('[data-checkme-ref="' + ref + '"]');
  if (!el) return { ok: false, error: 'ref not found: ' + ref };
  const fire = (name) => el.dispatchEvent(new Event(name, { bubbles: true }));
  if (kind === 'click' || kind === 'check' || kind === 'uncheck') {
    el.click();
    return { ok: true };
  }
  if (kind === 'dblclick') {
    el.dispatchEvent(new MouseEvent('dblclick', { bubbles: true }));
    return { ok: true };
  }
  if (kind === 'hover') {
    el.dispatchEvent(new MouseEvent('mouseover', { bubbles: true }));
    return { ok: true };
  }
  if (kind === 'fill' || kind === 'type' || kind === 'select' || kind === 'pick') {
    el.focus();
    if ('value' in el) el.value = text || '';
    else el.textContent = text || '';
    fire('input');
    fire('change');
    if (kind === 'type' || kind === 'select' || kind === 'pick') {
      el.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    }
    return { ok: true };
  }
  if (kind === 'press') {
    const key = text || 'Enter';
    el.dispatchEvent(new KeyboardEvent('keydown', { key: key, bubbles: true }));
    el.dispatchEvent(new KeyboardEvent('keyup', { key: key, bubbles: true }));
    return { ok: true };
  }
  el.click();
  return { ok: true };
})
