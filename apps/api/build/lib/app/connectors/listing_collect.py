"""Scroll a listing, keep structured cards, and read the reviews on matching product pages.

The page snapshot only keeps the head and tail of the text and 150 controls, so a long grid
never reaches the model. This walks the live page instead. No site name or selector is built in.

The same scripts ship in the desktop sidecar (apps/desktop/src-tauri/src/collect.js).
"""

from __future__ import annotations

import re

MAX_PASSES = 80
CARD_CAP = 400
REVIEW_BATCH = 80
REVIEW_SNIPPETS = 8

# Accessible names we may click once to reveal reviews, and names we must never click.
REVIEW_BAD = r"buy|add to cart|add to bag|\bpay\b|submit|place order|checkout"
REVIEW_GOOD = r"\breviews?\b|\bratings?\b"
_BAD_RE = re.compile(REVIEW_BAD, re.I)
_GOOD_RE = re.compile(REVIEW_GOOD, re.I)

# Shared with the desktop sidecar. Markers let the sidecar pick each script out of collect.js.
EXTRACT_JS = r"""(() => {
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
})()"""

SCROLL_JS = r"""(() => {
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
})()"""

# click is true on the first read (open a reviews control if none are visible) and false after that click.
REVIEW_FN = r"""(click) => {
  /* lumio-collect-reviews */
  const CUR = '(?:\\u20b9|Rs\\.?|INR|USD|EUR|GBP|\\$|\\u20ac|\\u00a3)';
  const PRICE = new RegExp(CUR + '\\s*(\\d{1,3}(?:[, ]\\d{2,3})+|\\d+(?:\\.\\d{1,2})?)|(\\d{1,3}(?:[, ]\\d{2,3})+|\\d+(?:\\.\\d{1,2})?)\\s*' + CUR, 'gi');
  const bad = /__BAD__/i;
  const good = /__GOOD__/i;
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
}"""

REVIEW_FN = REVIEW_FN.replace("__BAD__", REVIEW_BAD).replace("__GOOD__", REVIEW_GOOD)
REVIEW_JS = "(" + REVIEW_FN + ")(true)"
REVIEW_READ_JS = "(" + REVIEW_FN + ")(false)"

_CACHE: dict[int, dict] = {}


def clear_collect_cache() -> None:
    _CACHE.clear()


def review_control_ok(name: str) -> bool:
    """True when a control's accessible name is a reviews/ratings toggle and not a purchase action."""
    text = " ".join((name or "").split())
    if not text or len(text) > 60:
        return False
    if _BAD_RE.search(text) or not _GOOD_RE.search(text):
        return False
    return True


def _num(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return number


def split_prices(amounts) -> tuple[float | None, float | None, list[float]]:
    """Struck-through amounts are MRP. A non-struck amount is the selling price.

    Extra amounts stay in other_prices. When the card has only one kind of amount, that
    amount is the selling price (the only price on the card).
    """
    struck: list[float] = []
    plain: list[float] = []
    for item in amounts or []:
        if not isinstance(item, dict):
            continue
        value = _num(item.get("value"))
        if value is None or value <= 0:
            continue
        if item.get("struck"):
            struck.append(value)
        else:
            plain.append(value)
    other: list[float] = []
    selling = None
    mrp = None
    if plain:
        selling = plain[0]
        other.extend(plain[1:])
        if struck:
            mrp = max(struck)
            other.extend(v for v in struck if v != mrp)
    elif struck:
        selling = struck[0]
        other.extend(struck[1:])
    return selling, mrp, other


def canon_url(url: str) -> str:
    text = (url or "").strip()
    if not text.lower().startswith(("http://", "https://")):
        return ""
    return text.split("#", 1)[0]


def _words(raw: str) -> list[str]:
    return [part.lower() for part in (raw or "").split() if part.strip()]


def _title_ok(title: str, words: list[str]) -> bool:
    if not words:
        return True
    low = (title or "").lower()
    return all(word in low for word in words)


def _in_range(price: float | None, low: float | None, high: float | None) -> bool:
    if price is None:
        return False
    if low is not None and price < low:
        return False
    if high is not None and price > high:
        return False
    return True


def _row_from(card: dict) -> dict | None:
    url = canon_url(str(card.get("url") or ""))
    title = " ".join(str(card.get("title") or "").split())
    if not url or len(title) < 2:
        return None
    selling, mrp, other = split_prices(card.get("amounts") or [])
    if selling is None:
        return None
    return {
        "title": title[:180],
        "url": url,
        "selling_price": selling,
        "mrp": mrp,
        "other_prices": other,
        "rating": _num(card.get("rating")),
        "rating_count": card.get("rating_count"),
        "review_count": card.get("review_count"),
    }


def filter_cards(cards, *, min_price=None, max_price=None, words: str = "") -> tuple[list[dict], int, int]:
    """Return (matches, unpriced, scanned). Unpriced cards are counted and not returned as matches."""
    low = _num(min_price)
    high = _num(max_price)
    needed = _words(words)
    seen: dict[str, dict] = {}
    order: list[str] = []
    unpriced = 0
    for card in cards or []:
        if not isinstance(card, dict):
            continue
        url = canon_url(str(card.get("url") or ""))
        if not url:
            continue
        if url in seen:
            continue
        row = _row_from(card)
        if row is None:
            unpriced += 1
            seen[url] = {}
            continue
        seen[url] = row
        if not _title_ok(row["title"], needed):
            continue
        if not _in_range(row["selling_price"], low, high):
            continue
        order.append(url)
    return [seen[url] for url in order], unpriced, len(seen)


def _pause(page) -> None:
    wait = getattr(page, "wait_for_timeout", None)
    if not callable(wait):
        return
    try:
        wait(400)
    except Exception:
        return


def _eval(page, script: str):
    try:
        return page.evaluate(script)
    except Exception:
        return None


def scroll_collect(page, *, max_cards: int = CARD_CAP, settle=None) -> tuple[list[dict], str]:
    """Scroll until two passes add nothing and the page stops moving, or the card cap is hit.

    A static page has a fixed scroll height while scrollTop still advances, so height alone
    is not the end of the list. `moved` is. Two empty passes while the scroller still moves
    means the new cards were not readable (stalled).
    """
    seen: dict[str, dict] = {}
    order: list[str] = []
    stall = 0
    stopped = "end"
    cap = max(1, int(max_cards or CARD_CAP))
    for _ in range(MAX_PASSES):
        found = _eval(page, EXTRACT_JS) or []
        added = 0
        if isinstance(found, list):
            for card in found:
                if not isinstance(card, dict):
                    continue
                url = canon_url(str(card.get("url") or ""))
                if not url or url in seen:
                    continue
                seen[url] = card
                order.append(url)
                added += 1
        if len(order) >= cap:
            stopped = "cap"
            break
        state = _eval(page, SCROLL_JS) or {}
        if not isinstance(state, dict):
            state = {}
        moved = bool(state.get("moved"))
        if settle:
            try:
                settle(page)
            except Exception:
                pass
        else:
            _pause(page)
        if added == 0:
            stall += 1
        else:
            stall = 0
        if stall >= 2 and not moved:
            stopped = "end"
            break
        if stall >= 2 and moved:
            stopped = "stalled"
            break
    else:
        stopped = "stalled" if len(order) < cap else "cap"
    return [seen[url] for url in order], stopped


def _apply_review(row: dict, info: dict) -> None:
    selling, mrp, other = split_prices(info.get("amounts") or [])
    if selling is not None and row.get("selling_price") not in (None, selling):
        extra = list(row.get("other_prices") or [])
        if row.get("selling_price") is not None:
            extra.append(row["selling_price"])
        extra.extend(other)
        row["other_prices"] = extra
        row["selling_price"] = selling
    elif other:
        row["other_prices"] = list(row.get("other_prices") or []) + other
    if mrp is not None:
        row["mrp"] = mrp
    if _num(info.get("rating")) is not None:
        row["rating"] = _num(info.get("rating"))
    if info.get("rating_count") is not None:
        row["rating_count"] = info.get("rating_count")
    if info.get("review_count") is not None:
        row["review_count"] = info.get("review_count")
    reviews = []
    for text in info.get("reviews") or []:
        snippet = " ".join(str(text).split())
        if snippet:
            reviews.append(snippet[:400])
        if len(reviews) >= REVIEW_SNIPPETS:
            break
    row["reviews"] = reviews
    row["reviews_shown"] = len(reviews)
    row["reviews_complete"] = False


def _open(page, url: str, settle) -> str:
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
    except TypeError:
        page.goto(url)
    except Exception as exc:
        return str(exc)[:160]
    if settle:
        try:
            settle(page)
        except Exception:
            pass
    else:
        _pause(page)
    return ""


def _read_reviews(page, row: dict, settle) -> None:
    err = _open(page, row["url"], settle)
    if err:
        row["reviews"] = []
        row["reviews_shown"] = 0
        row["reviews_complete"] = False
        row["review_error"] = err
        return
    info = _eval(page, REVIEW_JS) or {}
    if not isinstance(info, dict):
        info = {}
    if info.get("clicked"):
        if settle:
            try:
                settle(page)
            except Exception:
                pass
        else:
            _pause(page)
        info = _eval(page, REVIEW_READ_JS) or info
        if not isinstance(info, dict):
            info = {}
    _apply_review(row, info)


def collect_page(
    page,
    *,
    min_price=None,
    max_price=None,
    words: str = "",
    with_reviews: bool = False,
    offset: int = 0,
    max_cards: int = CARD_CAP,
    review_batch: int = REVIEW_BATCH,
    settle=None,
) -> dict:
    """Collect the listing in front of `page`, then optionally open each match for reviews.

    `offset` continues a review pass. The listing is scrolled again only when this is a new
    query (offset 0 or the filters changed).
    """
    try:
        start = max(0, int(offset or 0))
    except (TypeError, ValueError):
        start = 0
    batch = max(1, int(review_batch or REVIEW_BATCH))
    key = (str(_num(min_price)), str(_num(max_price)), " ".join(_words(words)), bool(with_reviews))
    cached = _CACHE.get(id(page))
    if start and cached and cached.get("key") == key:
        matched = cached["matched"]
        scanned = cached["scanned"]
        unpriced = cached["unpriced"]
        stopped = cached["stopped"]
    else:
        raw, stopped = scroll_collect(page, max_cards=max_cards, settle=settle)
        matched, unpriced, scanned = filter_cards(raw, min_price=min_price, max_price=max_price, words=words)
        _CACHE[id(page)] = {
            "key": key,
            "matched": matched,
            "scanned": scanned,
            "unpriced": unpriced,
            "stopped": stopped,
        }
    rows = matched
    next_offset = None
    if with_reviews:
        rows = [dict(row) for row in matched[start : start + batch]]
        for row in rows:
            _read_reviews(page, row, settle)
        nxt = start + len(rows)
        next_offset = nxt if nxt < len(matched) else None
    return {
        "ok": True,
        "scanned": scanned,
        "matched": len(matched),
        "unpriced": unpriced,
        "stopped": stopped,
        "rows": rows,
        "next_offset": next_offset,
        "note": (
            "Selling price is the non-struck amount, or the only amount on the card. "
            "It can still vary by size. Reviews are the ones shown on the page, not a full export."
        ),
    }
