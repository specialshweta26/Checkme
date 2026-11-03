/* lumio-collect-cards */
(() => {
  /* lumio-collect-cards */
  const CUR = '(?:\\u20b9|Rs\\.?|INR|USD|EUR|GBP|\\$|\\u20ac|\\u00a3)';
  const PRICE = new RegExp(CUR + '\\s*(\\d{1,3}(?:[, ]\\d{2,3})+|\\d+(?:\\.\\d{1,2})?)|(\\d{1,3}(?:[, ]\\d{2,3})+|\\d+(?:\\.\\d{1,2})?)\\s*' + CUR, 'gi');
  const num = (raw) => {
    const n = parseFloat(String(raw || '').replace(/,/g, '').replace(/\s/g, ''));
    return Number.isFinite(n) && n > 0 && n < 1e8 ? n : null;
  };
  const struck = (node) => {
    let el = node && node.parentElement;
    for (let i = 0; i < 5 && el; i++) {
      const tag = el.tagName;
      if (tag === 'S' || tag === 'DEL' || tag === 'STRIKE') return true;
      const st = getComputedStyle(el);
      const deco = (st.textDecorationLine || st.textDecoration || '') + '';
      if (deco.indexOf('line-through') >= 0) return true;
      el = el.parentElement;
    }
    return false;
  };
  const amountsFrom = (root) => {
    const out = [];
    if (!root) return out;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = walker.nextNode())) {
      const text = n.nodeValue || '';
      PRICE.lastIndex = 0;
      let m;
      while ((m = PRICE.exec(text))) {
        const value = num(m[1] || m[2]);
        if (value == null) continue;
        out.push({ value, struck: struck(n) });
      }
    }
    return out;
  };
  const absUrl = (href) => {
    try {
      const u = new URL(href, location.href);
      if (u.protocol !== 'http:' && u.protocol !== 'https:') return '';
      if (!u.pathname || u.pathname === '/') return '';
      u.hash = '';
      return u.href;
    } catch (e) { return ''; }
  };
  const inChrome = (el) => {
    let n = el;
    for (let i = 0; i < 8 && n; i++) {
      const tag = (n.tagName || '').toLowerCase();
      const role = ((n.getAttribute && n.getAttribute('role')) || '').toLowerCase();
      if (tag === 'nav' || tag === 'header' || tag === 'footer' || role === 'navigation' || role === 'banner' || role === 'contentinfo') return true;
      n = n.parentElement;
    }
    return false;
  };
  const rateOf = (blob, aria) => {
    const hay = (aria || '') + ' ' + (blob || '');
    const rate = hay.match(/([0-5](?:\.\d)?)\s*(?:\/\s*5|out of 5|stars?)/i) || hay.match(/rated\s+([0-5](?:\.\d)?)/i);
    const rc = (blob || '').match(/(\d[\d,]*)\s+ratings?/i);
    const rv = (blob || '').match(/(\d[\d,]*)\s+reviews?/i);
    return {
      rating: rate ? parseFloat(rate[1]) : null,
      rating_count: rc ? parseInt(rc[1].replace(/,/g, ''), 10) : null,
      review_count: rv ? parseInt(rv[1].replace(/,/g, ''), 10) : null,
    };
  };
  const cards = [];
  const seen = new Set();
  for (const a of document.querySelectorAll('a[href]')) {
    const url = absUrl(a.href);
    if (!url || seen.has(url) || inChrome(a)) continue;
    let el = a;
    let card = null;
    for (let i = 0; i < 7 && el && el !== document.body; i++) {
      const text = (el.innerText || '').replace(/\s+/g, ' ').trim();
      if (text.length > 20 && text.length < 800 && amountsFrom(el).length) card = el;
      el = el.parentElement;
    }
    if (!card || !card.parentElement) continue;
    let siblings = 0;
    for (const child of card.parentElement.children) {
      if (child.querySelector && child.querySelector('a[href]') && amountsFrom(child).length) siblings += 1;
    }
    if (siblings < 2) continue;
    const amounts = amountsFrom(card);
    if (!amounts.length) continue;
    const heading = card.querySelector('h1,h2,h3,h4,h5');
    let title = ((heading && heading.innerText) || a.innerText || '').replace(/\s+/g, ' ').trim();
    if (title.length > 180) title = title.slice(0, 180);
    if (title.length < 2) continue;
    const blob = (card.innerText || '').replace(/\s+/g, ' ');
    const aria = Array.from(card.querySelectorAll('[aria-label]')).map((x) => x.getAttribute('aria-label') || '').join(' ');
    const rate = rateOf(blob, aria);
    seen.add(url);
    cards.push({ title, url, amounts, rating: rate.rating, rating_count: rate.rating_count, review_count: rate.review_count });
  }
  return cards.slice(0, 500);
})()
/* lumio-collect-end */
/* lumio-collect-scroll */
(() => {
  /* lumio-collect-scroll */
  const doc = document.scrollingElement || document.documentElement;
  let best = doc;
  let extra = best.scrollHeight - best.clientHeight;
  let n = 0;
  for (const el of document.querySelectorAll('main, [role="main"], section, div')) {
    if (++n > 500) break;
    if (!el || el.clientHeight < 200) continue;
    const oy = getComputedStyle(el).overflowY;
    if (oy !== 'auto' && oy !== 'scroll') continue;
    const more = el.scrollHeight - el.clientHeight;
    if (more > extra + 80) { best = el; extra = more; }
  }
  const before = best.scrollTop || 0;
  const height = best.scrollHeight || 0;
  const step = Math.max(400, Math.round((best.clientHeight || 600) * 0.85));
  best.scrollBy(0, step);
  const top = best.scrollTop || 0;
  return { top, height, moved: top > before + 10 };
})()
/* lumio-collect-end */
/* lumio-collect-review-fn */
(click) => {
  /* lumio-collect-reviews */
  const CUR = '(?:\\u20b9|Rs\\.?|INR|USD|EUR|GBP|\\$|\\u20ac|\\u00a3)';
  const PRICE = new RegExp(CUR + '\\s*(\\d{1,3}(?:[, ]\\d{2,3})+|\\d+(?:\\.\\d{1,2})?)|(\\d{1,3}(?:[, ]\\d{2,3})+|\\d+(?:\\.\\d{1,2})?)\\s*' + CUR, 'gi');
  const bad = /buy|add to cart|add to bag|\bpay\b|submit|place order|checkout/i;
  const good = /\breviews?\b|\bratings?\b/i;
  const num = (raw) => {
    const n = parseFloat(String(raw || '').replace(/,/g, '').replace(/\s/g, ''));
    return Number.isFinite(n) && n > 0 && n < 1e8 ? n : null;
  };
  const struck = (node) => {
    let el = node && node.parentElement;
    for (let i = 0; i < 5 && el; i++) {
      const tag = el.tagName;
      if (tag === 'S' || tag === 'DEL' || tag === 'STRIKE') return true;
      if (((getComputedStyle(el).textDecorationLine || '') + '').indexOf('line-through') >= 0) return true;
      el = el.parentElement;
    }
    return false;
  };
  const amountsFrom = (root) => {
    const out = [];
    if (!root) return out;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = walker.nextNode())) {
      PRICE.lastIndex = 0;
      let m;
      while ((m = PRICE.exec(n.nodeValue || ''))) {
        const value = num(m[1] || m[2]);
        if (value == null) continue;
        out.push({ value, struck: struck(n) });
      }
    }
    return out;
  };
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    const st = getComputedStyle(el);
    return st.visibility !== 'hidden' && st.display !== 'none';
  };
  const snippets = () => {
    let best = [];
    let seen = 0;
    for (const p of document.querySelectorAll('ul, ol, section, div')) {
      if (++seen > 800) break;
      const kids = Array.from(p.children).map((k) => (k.innerText || '').replace(/\s+/g, ' ').trim()).filter((t) => t.length > 40 && t.length < 1200);
      if (kids.length < 2) continue;
      const reviewish = kids.filter((t) => /review|rating|star|verified/i.test(t)).length;
      if (reviewish < 1 && kids.length < 3) continue;
      if (kids.length > best.length) best = kids;
    }
    return best.slice(0, 8);
  };
  const productAmounts = () => {
    const h = document.querySelector('h1');
    if (!h) return amountsFrom(document.querySelector('main, [role="main"]') || document.body);
    let el = h;
    let best = [];
    for (let i = 0; i < 5 && el && el !== document.body; i++) {
      if ((el.innerText || '').length < 4000) {
        const got = amountsFrom(el);
        if (got.length) best = got;
      }
      el = el.parentElement;
    }
    return best;
  };
  const ratingOf = () => {
    const aria = Array.from(document.querySelectorAll('[aria-label]')).slice(0, 40).map((el) => el.getAttribute('aria-label') || '').join(' ');
    const blob = ((document.body && document.body.innerText) || '').slice(0, 2500);
    const hay = aria + ' ' + blob;
    const rate = hay.match(/([0-5](?:\.\d)?)\s*(?:\/\s*5|out of 5|stars?)/i) || hay.match(/rated\s+([0-5](?:\.\d)?)/i);
    const rc = blob.match(/(\d[\d,]*)\s+ratings?/i);
    const rv = blob.match(/(\d[\d,]*)\s+reviews?/i);
    return {
      rating: rate ? parseFloat(rate[1]) : null,
      rating_count: rc ? parseInt(rc[1].replace(/,/g, ''), 10) : null,
      review_count: rv ? parseInt(rv[1].replace(/,/g, ''), 10) : null,
    };
  };
  const found = snippets();
  if (click && found.length < 1) {
    for (const el of document.querySelectorAll('a, button, [role="tab"], [role="button"]')) {
      if (!visible(el)) continue;
      const name = (el.getAttribute('aria-label') || el.innerText || '').replace(/\s+/g, ' ').trim();
      if (!name || name.length > 60 || bad.test(name) || !good.test(name)) continue;
      el.click();
      return { clicked: true, reviews: [], amounts: [], rating: null, rating_count: null, review_count: null };
    }
  }
  const rate = ratingOf();
  return { clicked: false, reviews: found, amounts: productAmounts(), rating: rate.rating, rating_count: rate.rating_count, review_count: rate.review_count };
}
/* lumio-collect-end */
