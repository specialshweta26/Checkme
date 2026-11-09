// Keeps pop-ups from blocking the page and notes errors, so the agent can be told about them. Installed once per page.
(function () {
  if (window.__lumioWatch) return true;
  const watch = { dialogs: [], errors: [], acceptNext: false, answer: null };
  const keep = (list, item, cap) => { list.push(item); if (list.length > cap) list.shift(); };
  const text = (v) => String(v === undefined || v === null ? '' : v).slice(0, 300);
  window.alert = (message) => { keep(watch.dialogs, { type: 'alert', message: text(message), answer: 'ok' }, 10); };
  window.confirm = (message) => {
    const yes = watch.acceptNext;
    watch.acceptNext = false;
    keep(watch.dialogs, { type: 'confirm', message: text(message), answer: yes ? 'accepted' : 'declined' }, 10);
    return yes;
  };
  window.prompt = (message, fallback) => {
    const yes = watch.acceptNext;
    const value = yes ? (watch.answer === null ? (fallback === undefined ? '' : String(fallback)) : watch.answer) : null;
    watch.acceptNext = false;
    watch.answer = null;
    keep(watch.dialogs, { type: 'prompt', message: text(message), answer: yes ? 'accepted' : 'declined' }, 10);
    return value;
  };
  const original = console.error;
  console.error = function () {
    keep(watch.errors, text(Array.from(arguments).map(String).join(' ')), 30);
    return original.apply(this, arguments);
  };
  window.addEventListener('error', (e) => keep(watch.errors, text(e.message), 30));
  window.addEventListener('unhandledrejection', (e) => keep(watch.errors, text(e.reason), 30));
  watch.accept = (answer) => { watch.acceptNext = true; watch.answer = answer ? String(answer) : null; return true; };
  watch.drain = () => watch.dialogs.splice(0);
  watch.log = () => {
    const strip = (url) => String(url).split(/[?#]/)[0].slice(0, 200);
    const failed = performance.getEntriesByType('resource')
      .filter((r) => r.responseStatus >= 400)
      .slice(-30)
      .map((r) => ({ url: strip(r.name), status: r.responseStatus }));
    return { errors: watch.errors.slice(), failed };
  };
  window.__lumioWatch = watch;
  return true;
})()
