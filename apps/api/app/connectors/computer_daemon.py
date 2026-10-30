"""Persistent Chromium on the cloud computer (Lumio page-browser pattern). Headed on E2B Desktop, headless elsewhere."""

from __future__ import annotations

import hashlib
import json
import os
import queue
import random
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = "127.0.0.1"
PORT = 18765
VERSION = "20"
_HOME = Path(os.environ.get("CHECKME_BROWSER_HOME") or "/tmp")
SHOT = "/tmp/checkme-browser.png"
PROFILE = str(_HOME / "profile")
LAST = str(_HOME / "last.json")
DOWNLOAD_DIR = Path(os.environ.get("CHECKME_DOWNLOADS") or "/tmp/checkme-downloads")
_UPLOAD_ROOTS = ("/tmp", "/home", "/var/data", str(_HOME))
_UPLOAD_MAX_B = 25 * 1024 * 1024
_downloads: list[dict] = []
# Written by the API before it starts the daemon (locale, timezone, proxy, channel...). Read once, then deleted so
# proxy credentials do not sit on disk. See docs/BROWSING.md.
CONFIG_PATH = "/tmp/checkme_browser_config.json"
FRESH_MARK = str(_HOME / "profile" / ".checkme-created")
VIEW_W = 1280
VIEW_H = 800
GOOGLE_HOME = "https://www.google.com/"
READY_HTML = (
    "<!doctype html><html><head><meta charset='utf-8'><title>Computer</title>"
    "<style>"
    "html,body{margin:0;height:100%;background:#0b1220;color:#f8fafc;"
    "font:22px ui-sans-serif,system-ui}"
    "body{display:flex;flex-direction:column;min-height:100%}"
    "header{flex:1;display:grid;place-items:center;padding:24px}"
    "h1{margin:0 0 8px;font-size:42px;letter-spacing:-.03em}"
    "p{margin:0;color:#94a3b8;font-size:18px}"
    "footer{height:48px;background:#020617;border-top:1px solid #1e293b}"
    "</style></head><body><header><div><h1>Computer is on</h1>"
    "<p>This is the live desktop. Pages you open appear here.</p></div></header>"
    "<footer></footer></body></html>"
)

_lock = threading.Lock()
# Playwright's sync API is bound to the thread that started it, and ThreadingHTTPServer answers every request on a
# new thread: without this, every browser command after the first fails with "cannot switch to a different thread".
# A leftover asyncio loop on that thread makes start() raise "Sync API inside the asyncio loop" (Playwright's guard
# if get_running_loop() succeeds). All browser work runs on this one long-lived thread, with no running loop.
_browser_thread = None
_pending_pages: list = []
_pending_downloads: list = []
_saved_downloads: dict = {}


def asyncio_loop_is_running() -> bool:
    """True when this thread is inside a running asyncio loop (Playwright sync start() forbids that)."""
    import asyncio

    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


def prepare_thread_for_sync_playwright() -> None:
    """Drop an idle leftover loop so sync_playwright().start() can create its own. A running loop cannot be dropped."""
    import asyncio

    if asyncio_loop_is_running():
        return
    try:
        asyncio.set_event_loop(None)
    except Exception:
        pass


class _BrowserThread:
    """One worker: serial Playwright calls, started with no asyncio loop on the thread."""

    def __init__(self):
        self._jobs: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="checkme-browser", daemon=True)
        self._thread.start()

    def _run(self):
        prepare_thread_for_sync_playwright()
        while True:
            item = self._jobs.get()
            if item is None:
                return
            fn, args, box, done = item
            try:
                box.append(("ok", fn(*args)))
            except BaseException as exc:
                box.append(("err", exc))
            done.set()

    def submit(self, fn, *args):
        if threading.current_thread() is self._thread:
            return fn(*args)
        done = threading.Event()
        box: list = []
        self._jobs.put((fn, args, box, done))
        done.wait()
        kind, value = box[0]
        if kind == "err":
            raise value
        return value


def on_browser_thread(fn, *args):
    global _browser_thread
    with _lock:
        if _browser_thread is None:
            _browser_thread = _BrowserThread()

    def wrapped():
        try:
            return fn(*args)
        finally:
            drain_browser_events()

    return _browser_thread.submit(wrapped)


def _on_new_page(page) -> None:
    """Queue only: Playwright fires this while its driver loop is running, so sync APIs here raise."""
    _pending_pages.append(page)


def _on_download(item) -> None:
    _pending_downloads.append(item)


def drain_browser_events() -> None:
    """Register popups and save downloads after the current Playwright call returns."""
    while True:
        pages = list(_pending_pages)
        _pending_pages.clear()
        downloads = list(_pending_downloads)
        _pending_downloads.clear()
        if not pages and not downloads:
            return
        for page in pages:
            try:
                _register_tab(page, activate=False)
            except Exception:
                pass
        for item in downloads:
            try:
                _save_download(item)
            except Exception:
                pass


_browser_page = None
_browser_context = None
_elements: list[dict] = []
_elements_url = ""
# Tabs: id -> Page, the active tab (what the live screen shows), and each tab's last snapshot (refs are per tab).
_tabs: dict = {}
_active_tab = ""
_tab_seq = 0
_tab_refs: dict = {}

try:  # the API imports the package module; on the computer the same file sits next to this one
    from app.connectors.block_detect import detect_block
    from app.connectors.block_detect import page_is_thin as _thin
except ImportError:  # pragma: no cover - exercised on the cloud computer
    from checkme_block_detect import detect_block  # type: ignore
    from checkme_block_detect import page_is_thin as _thin  # type: ignore


def _load_config() -> dict:
    try:
        raw = json.loads(Path(CONFIG_PATH).read_text() or "{}")
    except Exception:
        return {}
    try:
        os.unlink(CONFIG_PATH)
    except Exception:
        pass
    return raw if isinstance(raw, dict) else {}


CONFIG: dict = _load_config() if __name__ == "__main__" else {}


def config_hash(cfg: dict | None) -> str:
    """Short fingerprint of a browser config (the API restarts the daemon when it changes). Never the secrets."""
    blob = json.dumps(cfg or {}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def page_is_thin(text: str) -> bool:
    return _thin(text)


def bot_wall(url: str = "", title: str = "", text: str = "", html: str = "", status: int | None = None) -> dict | None:
    """The page is a bot wall (CAPTCHA, JS check, access denied, rate limit), per the shared detector."""
    block = detect_block(status, url, title, text, html)
    if not block:
        return None
    kind = block["kind"]
    return {
        "bot_wall": True,
        "bot_wall_kind": "captcha" if kind == "captcha" else ("verification" if kind == "challenge" else kind),
        "block": block,
        # Only a CAPTCHA needs a person. Denied / unclear checks go to the fallback ladder first.
        "handoff": bool(block.get("interactive")),
        "reason": block["reason"],
    }


def annotate_page(data: dict | None) -> dict:
    row = data if isinstance(data, dict) else {}
    wall = bot_wall(str(row.get("url") or ""), str(row.get("title") or ""), str(row.get("text") or ""))
    if wall:
        row.update(wall)
        row["ok"] = False
        row["content_ready"] = False
        if wall.get("handoff"):
            row["hint"] = (
                f"{wall['reason']} Do not invent results. Ask the user to solve it on the live screen "
                "(request_takeover / browser_handoff), then snapshot and continue; meanwhile use other sites "
                "that cover the same information."
            )
        else:
            row["hint"] = (
                f"{wall['reason']} Do not invent results. Use the read-only copy if one is attached, else try "
                "other sites that cover the same information, and tell the user plainly which site blocked."
            )
    elif page_is_thin(str(row.get("text") or "")):
        row["content_ready"] = False
        row["hint"] = (
            "The page has not finished loading. Do not invent facts, prices, or results from this snapshot. "
            "Wait, snapshot again, or request_takeover if a challenge is showing."
        )
    else:
        row.setdefault("content_ready", True)
    return row


# Cookie / consent interstitials -------------------------------------------------------------------------------
# A person would click "Reject all" (or "Accept all" when that is the only choice) to get to the page. We do the
# same, only on pages that are clearly a consent prompt. Never used on CAPTCHAs or verification walls.
_CONSENT_CUES = (
    "before you continue",
    "we use cookies",
    "use of cookies",
    "cookie settings",
    "cookie preferences",
    "manage cookies",
    "your privacy choices",
    "consent",
    "gdpr",
)
_CONSENT_REJECT = re.compile(r"^\s*(reject all|reject|decline all|decline|only necessary|necessary only|use necessary cookies only|refuse all)\s*$", re.I)
_CONSENT_ACCEPT = re.compile(r"^\s*(accept all|accept|i agree|agree|allow all|accept cookies|accept all cookies|got it|ok)\s*$", re.I)


def looks_like_consent(url: str, text: str) -> bool:
    low = (text or "")[:5000].lower()
    host = (url or "").lower()
    if bot_wall(url, "", text):
        return False
    return "consent." in host or any(cue in low for cue in _CONSENT_CUES)


def _dismiss_consent(page) -> str:
    """Click the consent prompt's reject (else accept) button if one is showing. Returns what was clicked."""
    try:
        frames = [page.main_frame] + [f for f in page.frames if f != page.main_frame]
    except Exception:
        return ""
    for frame in frames[:8]:
        try:
            text = frame.evaluate("() => (document.body && document.body.innerText || '').slice(0, 5000)") or ""
        except Exception:
            continue
        if not looks_like_consent(frame.url or page.url, text):
            continue
        for pattern in (_CONSENT_REJECT, _CONSENT_ACCEPT):
            for role in ("button", "link"):
                try:
                    loc = frame.get_by_role(role, name=pattern)
                    if loc.count() and loc.first.is_visible():
                        label = (loc.first.inner_text() or "").strip()[:40]
                        loc.first.click(timeout=4000)
                        try:
                            page.wait_for_load_state("domcontentloaded", timeout=8000)
                        except Exception:
                            pass
                        page.wait_for_timeout(600)
                        return label or "consent"
                except Exception:
                    continue
    return ""


def _wait_content(page, max_ms: int = 8000) -> None:
    """Wait until the page's text stops growing (JS apps, maps and listings render after DOMContentLoaded)."""
    if max_ms <= 0:
        return
    try:
        page.wait_for_load_state("networkidle", timeout=min(4000, max_ms))
    except Exception:
        pass
    last, stable, waited = -1, 0, 0
    while waited < max_ms:
        try:
            n = int(page.evaluate("() => (document.body && document.body.innerText || '').length") or 0)
        except Exception:
            return
        if n == last and n >= 180:
            stable += 1
            if stable >= 2:
                return
        else:
            stable = 0
        last = n
        page.wait_for_timeout(500)
        waited += 500


# Search result pages in the real browser -------------------------------------------------------------------------
SERP_URLS = {
    "bing": "https://www.bing.com/search?q={q}&setlang=en",
    "duckduckgo": "https://html.duckduckgo.com/html/?q={q}",
    "google": "https://www.google.com/search?q={q}&hl=en",
}
_SERP_ERROR = ("if this persists, please email us", "if error persists, please email us", "unexpected error. please try again")
_SERP_JS = """(limit) => {
  const host = location.hostname;
  const rows = [];
  const add = (a, snipEl) => {
    if (!a) return;
    const title = (a.innerText || a.textContent || '').replace(/\\s+/g, ' ').trim();
    const href = a.href || '';
    if (!title || !/^https?:/.test(href)) return;
    rows.push({title: title.slice(0, 200), url: href, snippet: ((snipEl && snipEl.innerText) || '').replace(/\\s+/g, ' ').trim().slice(0, 400)});
  };
  if (host.includes('bing.')) {
    document.querySelectorAll('li.b_algo').forEach((li) => add(li.querySelector('h2 a'), li.querySelector('.b_caption p, p')));
  } else if (host.includes('duckduckgo.')) {
    document.querySelectorAll('.result, article[data-testid="result"]').forEach((r) => add(
      r.querySelector('a.result__a, a[data-testid="result-title-a"], h2 a'),
      r.querySelector('.result__snippet, [data-result="snippet"]')));
  } else {
    document.querySelectorAll('#search a, #rso a').forEach((a) => {
      const h = a.querySelector('h3');
      if (!h) return;
      const box = a.closest('div.g, div[data-hveid], div[data-snc]');
      const sn = box && box.querySelector('.VwiC3b, [data-sncf], [style*="-webkit-line-clamp"]');
      rows.push({title: h.innerText.trim().slice(0, 200), url: a.href, snippet: ((sn && sn.innerText) || '').trim().slice(0, 400)});
    });
  }
  return rows.slice(0, limit * 2);
}"""


def _serp(cmd: dict) -> dict:
    """Run one search in a background tab of the real browser and read the results from the DOM."""
    from urllib.parse import quote_plus

    engine = str(cmd.get("engine") or "bing").lower()
    query = str(cmd.get("query") or "").strip()
    limit = max(1, min(int(cmd.get("count") or 8), 12))
    if engine not in SERP_URLS or not query:
        raise RuntimeError("serp needs engine (bing|duckduckgo|google) and query")
    _ensure_page()
    page = _browser_context.new_page()
    try:
        url = SERP_URLS[engine].format(q=quote_plus(query))
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(800)
        consent = _dismiss_consent(page)
        _wait_content(page, 5000)
        try:
            text = page.inner_text("body")[:4000]
        except Exception:
            text = ""
        wall = bot_wall(page.url, page.title(), text)
        if wall:
            return {"ok": False, "engine": engine, "blocked": wall.get("bot_wall_kind") or "verification", "url": page.url}
        low = text.lower()
        if any(cue in low for cue in _SERP_ERROR):
            return {"ok": False, "engine": engine, "blocked": "error page", "url": page.url}
        rows = page.evaluate(_SERP_JS, limit) or []
        return {"ok": True, "engine": engine, "url": page.url, "results": rows, "consent": consent}
    finally:
        try:
            page.close()
        except Exception:
            pass
        try:
            if _browser_page is not None:
                _browser_page.bring_to_front()
        except Exception:
            pass


def _dump(page):
    page.screenshot(path=SHOT, full_page=False)
    try:
        text = page.inner_text("body")
    except Exception:
        text = ""
    Path(LAST).write_text(
        json.dumps({"url": page.url, "title": page.title(), "viewport": {"width": VIEW_W, "height": VIEW_H}})
    )
    return annotate_page(
        {
            "ok": True,
            "url": page.url,
            "title": page.title(),
            "text": (text or "")[:12000],
            "viewport": {"width": VIEW_W, "height": VIEW_H},
            "screenshot_path": SHOT,
            "screenshot_name": "page.png",
        }
    )


# Snapshot collector: open overlays / listboxes first, then other controls. Walks open shadow roots.
# start is the first ref index so iframes can share one e0..eN sequence.
_SNAPSHOT_JS = """(start) => {
  const BASE = 'a, button, input, textarea, select, option, label, summary, [role="button"], [role="link"], [role="textbox"], [role="searchbox"], [role="checkbox"], [role="radio"], [role="combobox"], [role="option"], [role="listbox"], [role="tab"], [role="menuitem"], [role="menuitemradio"], [role="treeitem"], [role="row"], [contenteditable="true"]';
  const SUGGEST = '[role="option"], [role="menuitem"], [role="menuitemradio"], [role="treeitem"], [role="listbox"] li, [role="listbox"] button, [role="menu"] li, [role="menu"] button, [role="dialog"] li, [role="listbox"] [tabindex]';
  const tags = {A: 'link', BUTTON: 'button', INPUT: 'textbox', TEXTAREA: 'textbox', SELECT: 'combobox', OPTION: 'option', LABEL: 'label', SUMMARY: 'button', LI: 'option'};
  const inputRole = {submit: 'button', button: 'button', reset: 'button', image: 'button', checkbox: 'checkbox', radio: 'radio', search: 'searchbox', range: 'slider', file: 'file'};
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return false;
    const st = window.getComputedStyle(el);
    return st.visibility !== 'hidden' && st.display !== 'none' && Number(st.opacity || 1) > 0.05;
  };
  const inView = (el) => { const r = el.getBoundingClientRect(); return r.bottom > 0 && r.top < window.innerHeight; };
  const overlayRoot = (el) => {
    const role = (el.getAttribute('role') || '').toLowerCase();
    if (['listbox', 'menu', 'dialog', 'alertdialog', 'list'].includes(role)) return true;
    const st = window.getComputedStyle(el);
    const z = parseInt(st.zIndex, 10);
    return (st.position === 'fixed' || st.position === 'absolute') && Number.isFinite(z) && z >= 10 && el.getBoundingClientRect().height > 24;
  };
  const collect = (root, selector) => {
    const out = [];
    const visit = (node) => {
      if (!node || !node.querySelectorAll) return;
      for (const el of node.querySelectorAll(selector)) out.push(el);
      for (const el of node.querySelectorAll('*')) {
        if (el.shadowRoot) visit(el.shadowRoot);
      }
    };
    visit(root);
    return out;
  };
  document.querySelectorAll('[data-checkme-ref]').forEach((el) => el.removeAttribute('data-checkme-ref'));
  const suggest = [];
  const rest = [];
  const seen = new Set();
  const add = (el, bucket) => {
    if (!el || seen.has(el) || !visible(el)) return;
    seen.add(el);
    bucket.push(el);
  };
  for (const el of collect(document, SUGGEST)) add(el, suggest);
  for (const el of collect(document, '*')) {
    if (!overlayRoot(el) || !visible(el)) continue;
    for (const child of collect(el, BASE + ', li, button, [tabindex]:not([tabindex="-1"])')) add(child, suggest);
  }
  for (const el of collect(document, BASE)) add(el, rest);
  const ordered = suggest.filter(inView).concat(suggest.filter((el) => !inView(el)))
    .concat(rest.filter(inView)).concat(rest.filter((el) => !inView(el)));
  const origin = Number(start) || 0;
  return ordered.slice(0, 150).map((el, i) => {
    const ref = 'e' + (origin + i);
    try { el.setAttribute('data-checkme-ref', ref); } catch (e) {}
    const name = (el.getAttribute('aria-label') || el.innerText || el.getAttribute('placeholder') || el.value || el.getAttribute('title') || el.getAttribute('name') || '').replace(/\\s+/g, ' ').trim().slice(0, 80);
    const r = el.getBoundingClientRect();
    const role = (el.getAttribute('role') || (el.tagName === 'INPUT' && inputRole[(el.getAttribute('type') || '').toLowerCase()]) || tags[el.tagName] || el.tagName.toLowerCase());
    return {
      ref, role, name,
      tag: el.tagName.toLowerCase(),
      href: el.href || '',
      input_type: el.getAttribute('type') || '',
      in_view: inView(el),
      overlay: suggest.includes(el),
      x: Math.round(r.x + r.width / 2),
      y: Math.round(r.y + r.height / 2),
    };
  });
}"""

# Visible type-ahead / menu rows (open shadow roots included). Used while picking a suggestion.
_SUGGEST_JS = """() => {
  const SEL = '[role="option"], [role="menuitem"], [role="menuitemradio"], [role="treeitem"], [role="listbox"] li, [role="listbox"] button, [role="menu"] li, [role="menu"] button, [role="dialog"] li, [role="listbox"] [tabindex]';
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return false;
    const st = window.getComputedStyle(el);
    return st.visibility !== 'hidden' && st.display !== 'none' && Number(st.opacity || 1) > 0.05;
  };
  const overlayRoot = (el) => {
    const role = (el.getAttribute('role') || '').toLowerCase();
    if (['listbox', 'menu', 'dialog', 'alertdialog', 'list'].includes(role)) return true;
    const st = window.getComputedStyle(el);
    const z = parseInt(st.zIndex, 10);
    return (st.position === 'fixed' || st.position === 'absolute') && Number.isFinite(z) && z >= 10 && el.getBoundingClientRect().height > 24;
  };
  const collect = (root, selector) => {
    const out = [];
    const visit = (node) => {
      if (!node || !node.querySelectorAll) return;
      for (const el of node.querySelectorAll(selector)) out.push(el);
      for (const el of node.querySelectorAll('*')) {
        if (el.shadowRoot) visit(el.shadowRoot);
      }
    };
    visit(root);
    return out;
  };
  const seen = new Set();
  const rows = [];
  const push = (el) => {
    if (!el || seen.has(el) || !visible(el)) return;
    seen.add(el);
    const name = (el.getAttribute('aria-label') || el.innerText || el.getAttribute('title') || '').replace(/\\s+/g, ' ').trim().slice(0, 120);
    if (name.length < 1) return;
    const r = el.getBoundingClientRect();
    rows.push({
      name,
      role: (el.getAttribute('role') || el.tagName || '').toLowerCase(),
      x: Math.round(r.x + r.width / 2),
      y: Math.round(r.y + r.height / 2),
    });
  };
  for (const el of collect(document, SEL)) push(el);
  for (const el of collect(document, '*')) {
    if (!overlayRoot(el) || !visible(el)) continue;
    for (const child of collect(el, 'li, button, a, [role="option"], [role="menuitem"], [tabindex]:not([tabindex="-1"])')) push(child);
  }
  return rows.slice(0, 40);
}"""


def _frames_of(page):
    try:
        frames = list(page.frames)
    except Exception:
        frames = []
    return frames[:16] if frames else [page]


def _snapshot(page):
    nodes: list[dict] = []
    start = 0
    for frame in _frames_of(page):
        try:
            part = frame.evaluate(_SNAPSHOT_JS, start)
        except Exception:
            continue
        if not isinstance(part, list):
            continue
        nodes.extend(n for n in part if isinstance(n, dict))
        start = len(nodes)
        if start >= 150:
            nodes = nodes[:150]
            break
    global _elements, _elements_url
    _elements = nodes
    _elements_url = page.url
    _tab_refs[id(page)] = (_elements, _elements_url)
    tree = "\n".join(f"- {n.get('ref')} {n.get('role')} {n.get('name') or ''}".strip() for n in _elements)
    data = _dump(page)
    data["tree"] = tree
    data["elements"] = _elements
    facts = _page_facts(page)
    if facts:
        data["verified"] = facts
        if facts.get("tables"):
            data["tables"] = facts["tables"]
        if facts.get("alerts"):
            data["alerts"] = facts["alerts"]
    if _downloads:
        data["downloads"] = list(_downloads[-8:])
    return annotate_page(data)


def _find(ref: str):
    for item in _elements:
        if item.get("ref") == ref:
            return item
    return None


def _has(loc) -> bool:
    if loc is None:
        return False
    try:
        return loc.count() > 0
    except Exception:
        return True


_REF = re.compile(r"^e\d+$")


def _locator(page, step, node):
    """Resolve a step's target. Snapshot refs map to the exact element the snapshot tagged
    (data-checkme-ref); fuzzy text matching is only for free-form selectors, and exact matches win,
    so a ref for a search box can never resolve to an unrelated link that merely contains the same word."""
    ref = str(step.get("ref") or step.get("selector") or "").strip()
    if ref and _REF.match(ref):
        loc = page.locator(f'[data-checkme-ref="{ref}"]').first
        if _has(loc):
            return loc
        if node and node.get("name"):
            role = str(node.get("role") or "generic")
            try:
                loc = page.get_by_role(role, name=node["name"], exact=True).first
            except Exception:
                loc = None
            if _has(loc):
                return loc
        return None
    if not ref:
        return None
    for factory in (
        lambda: page.locator(ref).first,
        lambda: page.get_by_label(ref, exact=True).first,
        lambda: page.get_by_placeholder(ref, exact=True).first,
        lambda: page.get_by_role("textbox", name=ref, exact=True).first,
        lambda: page.get_by_role("combobox", name=ref, exact=True).first,
        lambda: page.get_by_role("searchbox", name=ref, exact=True).first,
        lambda: page.get_by_role("option", name=ref, exact=False).first,
        lambda: page.get_by_role("menuitem", name=ref, exact=False).first,
        lambda: page.get_by_role("button", name=ref, exact=True).first,
        lambda: page.get_by_role("link", name=ref, exact=True).first,
        lambda: page.get_by_text(ref, exact=True).first,
        lambda: page.get_by_label(ref, exact=False).first,
        lambda: page.get_by_placeholder(ref, exact=False).first,
        lambda: page.get_by_text(ref, exact=False).first,
    ):
        try:
            loc = factory()
        except Exception:
            loc = None
        if _has(loc):
            return loc
    return None


def _settle(page, timeout: int = 8000) -> None:
    try:
        page.wait_for_load_state("domcontentloaded", timeout=timeout)
    except Exception:
        pass


_PAGE_FACTS_JS = """() => {
  const alerts = Array.from(document.querySelectorAll('[role="alert"], [aria-invalid="true"]'))
    .map((el) => (el.innerText || el.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim())
    .filter(Boolean).slice(0, 8);
  const fields = Array.from(document.querySelectorAll('input, textarea, select')).slice(0, 40).map((el) => {
    const type = (el.getAttribute('type') || el.tagName || '').toLowerCase();
    let value = el.value || '';
    if (type === 'password') value = value ? '••••' : '';
    return {
      name: (el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.getAttribute('name') || el.id || '').slice(0, 60),
      type,
      value: String(value).slice(0, 120),
      required: !!(el.required || el.getAttribute('aria-required') === 'true'),
    };
  }).filter((f) => f.name || f.value);
  const tables = Array.from(document.querySelectorAll('table')).slice(0, 3).map((t) => {
    const rows = Array.from(t.querySelectorAll('tr')).slice(0, 21).map((r) =>
      Array.from(r.querySelectorAll('th,td')).map((c) => (c.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 80))
    );
    return { headers: rows[0] || [], rows: rows.slice(1, 20) };
  });
  return { alerts, fields, tables };
}"""


def _page_facts(page) -> dict:
    try:
        row = page.evaluate(_PAGE_FACTS_JS)
    except Exception:
        return {}
    return row if isinstance(row, dict) else {}


def _act_page(page, actions: list):
    completed = 0
    extras: list[dict] = []
    try:
        for step in actions:
            extra = _act_step(page, step)
            if extra:
                extras.append(extra)
            completed += 1
            kind = str(step.get("kind") or "").lower()
            pause = 700 if kind in {"fill", "type", "select", "pick", "select_option", "upload", "download"} else 250
            page.wait_for_timeout(pause)
    except Exception as exc:
        _settle(page)
        data = _snapshot(page)
        data["ok"] = False
        data["completed"] = completed
        data["error"] = f"step {completed + 1} failed: {str(exc)[:400]}"
        data["hint"] = (
            "The page may have changed. Use the refs in this fresh snapshot (tree) and try again. "
            "For a type-ahead list, type into the field, wait, then click the matching option "
            "(or select/pick with the field ref and the value); if refs miss, computer_observe + "
            "computer_act at x,y or press ArrowDown then Enter. Do not request_takeover for a dropdown. "
            "Check verified.alerts after a failed fill."
        )
        _merge_extras(data, extras)
        return data
    _settle(page)
    data = _snapshot(page)
    data["completed"] = completed
    _merge_extras(data, extras)
    return data


def _merge_extras(data: dict, extras: list[dict]) -> None:
    downloads = list(data.get("downloads") or [])
    uploads = []
    for extra in extras:
        if extra.get("download"):
            downloads.append(extra["download"])
        if extra.get("upload"):
            uploads.append(extra["upload"])
    if downloads:
        data["downloads"] = downloads
    if uploads:
        data["uploads"] = uploads


def score_option(query: str, name: str) -> int:
    """How well a visible suggestion matches what the agent wants to pick (0 = no match)."""
    q = (query or "").strip().lower()
    n = (name or "").strip().lower()
    if not q or not n:
        return 0
    if n == q:
        return 100
    if f"({q})" in n or n.startswith(q + " ") or n.startswith(q + ","):
        return 92
    tokens = [t for t in re.split(r"[^a-z0-9]+", q) if t]
    if tokens and all(t in n for t in tokens):
        return max(50, 85 - max(0, len(n) - len(q)) // 8)
    if q in n:
        return 70
    hits = sum(1 for t in tokens if len(t) >= 3 and t in n)
    if hits:
        return 40 + hits * 8
    return 0


def best_option(query: str, candidates: list[dict]) -> dict | None:
    best = None
    best_score = 0
    for row in candidates or []:
        if not isinstance(row, dict):
            continue
        sc = score_option(query, str(row.get("name") or ""))
        if sc > best_score:
            best_score = sc
            best = row
    return best if best_score >= 40 else None


def looks_combobox(node: dict | None, info: dict | None = None) -> bool:
    role = str((node or {}).get("role") or "").lower()
    tag = str((node or {}).get("tag") or "").lower()
    if role in {"combobox", "searchbox", "listbox"}:
        return True
    if tag == "select":
        return False
    info = info or {}
    if str(info.get("role") or "").lower() in {"combobox", "searchbox"}:
        return True
    if str(info.get("ac") or "").lower() in {"list", "both", "inline"}:
        return True
    if str(info.get("popup") or "").lower() in {"listbox", "menu", "true"}:
        return True
    if info.get("list") or info.get("ce"):
        return True
    return False


def _combobox_info(loc) -> dict:
    if loc is None:
        return {}
    try:
        return loc.evaluate(
            """el => ({
              role: (el.getAttribute('role') || '').toLowerCase(),
              ac: (el.getAttribute('aria-autocomplete') || '').toLowerCase(),
              popup: (el.getAttribute('aria-haspopup') || '').toLowerCase(),
              list: el.getAttribute('list') || '',
              tag: (el.tagName || '').toLowerCase(),
              ce: !!el.isContentEditable,
            })"""
        ) or {}
    except Exception:
        return {}


def _click_target(page, loc=None, node=None, x=None, y=None, timeout: int = 12000) -> None:
    if loc is not None:
        try:
            loc.click(timeout=timeout)
            return
        except Exception:
            pass
    if x is None and node:
        x, y = node.get("x"), node.get("y")
    if x is not None and y is not None:
        page.mouse.click(int(x), int(y))
        return
    raise RuntimeError("nothing to click; take a new browser_snapshot")


def _suggestion_hits(page) -> list[dict]:
    hits: list[dict] = []
    for frame in _frames_of(page):
        try:
            rows = frame.evaluate(_SUGGEST_JS)
        except Exception:
            continue
        if isinstance(rows, list):
            hits.extend(r for r in rows if isinstance(r, dict))
    return hits


def _wait_suggestion(page, query: str, tries: int = 12) -> dict | None:
    best = None
    for _ in range(max(1, tries)):
        hit = best_option(query, _suggestion_hits(page))
        if hit is not None:
            sc = score_option(query, str(hit.get("name") or ""))
            if sc >= 60:
                return hit
            best = hit
        page.wait_for_timeout(200)
    return best


def _type_keys(page, text: str, replace: bool) -> None:
    if replace:
        page.keyboard.press("Control+A")
        page.keyboard.press("Backspace")
    page.keyboard.type(text, delay=random.randint(35, 70))


def _type_into(page, loc, node, text: str, replace: bool) -> None:
    info = _combobox_info(loc)
    suggest = looks_combobox(node, info)
    if suggest:
        _click_target(page, loc, node)
        page.wait_for_timeout(150)
        _type_keys(page, text, replace=True)
        page.wait_for_timeout(450)
        return
    if loc is not None:
        try:
            if replace:
                loc.fill(text, timeout=8000)
            else:
                loc.click(timeout=8000)
                page.keyboard.type(text, delay=random.randint(30, 70))
            return
        except Exception:
            loc.click(timeout=8000)
    elif node and node.get("x") is not None:
        page.mouse.click(int(node["x"]), int(node["y"]))
    _type_keys(page, text, replace)
    if replace:
        page.wait_for_timeout(120)


def pick_suggestion(page, loc, node, text: str) -> str:
    """Choose a value on a native <select> or a custom type-ahead list. Returns how it was picked."""
    value = str(text or "").strip()
    if not value:
        raise RuntimeError("select/pick needs text")
    tag = str((node or {}).get("tag") or "")
    info = _combobox_info(loc)
    if not tag:
        tag = str(info.get("tag") or "")
    if loc is not None and tag == "select":
        loc.select_option(value, timeout=12000)
        return "native"
    if loc is not None:
        try:
            loc.select_option(value, timeout=2500)
            return "native"
        except Exception:
            pass
    _click_target(page, loc, node)
    page.wait_for_timeout(200)
    _type_keys(page, value, replace=True)
    hit = _wait_suggestion(page, value)
    if hit is not None and hit.get("x") is not None:
        page.mouse.click(int(hit["x"]), int(hit["y"]))
        page.wait_for_timeout(200)
        return "option"
    page.keyboard.press("ArrowDown")
    page.wait_for_timeout(120)
    page.keyboard.press("Enter")
    page.wait_for_timeout(200)
    return "keyboard"


def safe_upload_path(raw: str) -> Path:
    """Only files on the computer workspace may be uploaded into the page."""
    text = str(raw or "").strip()
    if not text:
        raise RuntimeError("upload needs path")
    path = Path(text).expanduser()
    try:
        path = path.resolve()
    except Exception:
        raise RuntimeError(f"upload path is not usable: {text}") from None
    if not path.is_file():
        raise RuntimeError(f"upload file not found: {text}")
    allowed = False
    for root in _UPLOAD_ROOTS:
        try:
            path.relative_to(Path(root).expanduser().resolve())
            allowed = True
            break
        except Exception:
            continue
    if not allowed:
        raise RuntimeError("upload path is outside the computer workspace")
    if path.stat().st_size > _UPLOAD_MAX_B:
        raise RuntimeError("upload is larger than 25 MB")
    return path


def _unique_download(name: str) -> Path:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", (name or "download"))[:180] or "download"
    dest = DOWNLOAD_DIR / safe
    if not dest.exists():
        return dest
    stem, suffix = dest.stem, dest.suffix
    for i in range(2, 100):
        candidate = DOWNLOAD_DIR / f"{stem}-{i}{suffix}"
        if not candidate.exists():
            return candidate
    return DOWNLOAD_DIR / f"{stem}-{os.getpid()}{suffix}"


def remember_download(path: str, filename: str, url: str = "") -> dict:
    row = {"path": path, "filename": filename, "url": url}
    _downloads.append(row)
    return row


def _upload_file(page, loc, node, step: dict) -> dict:
    path = safe_upload_path(str(step.get("path") or step.get("file") or step.get("text") or ""))
    if loc is not None:
        try:
            loc.set_input_files(str(path), timeout=12000)
            return {"upload": {"path": str(path), "filename": path.name}}
        except Exception:
            pass
    chooser = getattr(page, "expect_file_chooser", None)
    if callable(chooser):
        with chooser(timeout=8000) as pending:
            _click_target(page, loc, node, step.get("x"), step.get("y"))
        pending.value.set_files(str(path))
        return {"upload": {"path": str(path), "filename": path.name}}
    raise RuntimeError("could not attach the file; snapshot a file input (role file) and retry")


def _save_download(download) -> dict:
    key = id(download)
    if key in _saved_downloads:
        return _saved_downloads[key]
    name = str(getattr(download, "suggested_filename", None) or "download")
    dest = _unique_download(name)
    download.save_as(str(dest))
    url = ""
    try:
        url = str(download.url or "")
    except Exception:
        pass
    row = remember_download(str(dest), dest.name, url)
    _saved_downloads[key] = row
    return row


def _click_download(page, loc, node, step: dict) -> dict:
    expect = getattr(page, "expect_download", None)
    if callable(expect):
        try:
            with expect(timeout=int(step.get("wait_ms") or 15000)) as pending:
                _click_target(page, loc, node, step.get("x"), step.get("y"))
            return {"download": _save_download(pending.value)}
        except Exception as exc:
            if _downloads:
                return {"download": _downloads[-1]}
            raise RuntimeError(f"download did not start: {str(exc)[:200]}") from exc
    _click_target(page, loc, node, step.get("x"), step.get("y"))
    if _downloads:
        return {"download": _downloads[-1]}
    raise RuntimeError("download did not start")


def _act_step(page, step: dict) -> dict | None:
    kind = str(step.get("kind") or "").lower()
    ref = str(step.get("ref") or step.get("selector") or "").strip()
    node = _find(ref) if ref else None
    loc = _locator(page, step, node)
    if loc is None and node is not None and page.url != _elements_url:
        # The page changed since the snapshot: its coordinates belong to another page.
        node = None
        if step.get("x") is None and kind not in {"press", "key", "scroll", "wait", "reload", "back", "forward"}:
            raise RuntimeError(f"ref {ref} is from an older page; take a new browser_snapshot")
    if kind == "click":
        _click_target(page, loc, node, step.get("x"), step.get("y"))
    elif kind in {"dblclick", "double_click", "doubleclick"}:
        if loc is not None:
            loc.dblclick(timeout=12000)
        elif node and node.get("x") is not None:
            page.mouse.dblclick(int(node["x"]), int(node["y"]))
        else:
            raise RuntimeError(f"unknown ref {ref}; take a new browser_snapshot")
    elif kind in {"rightclick", "contextclick", "context_click"}:
        if loc is not None:
            loc.click(button="right", timeout=12000)
        elif node and node.get("x") is not None:
            page.mouse.click(int(node["x"]), int(node["y"]), button="right")
        else:
            raise RuntimeError(f"unknown ref {ref}; take a new browser_snapshot")
    elif kind in {"fill", "type"}:
        _type_into(page, loc, node, str(step.get("text") or ""), kind == "fill")
    elif kind in {"select", "select_option", "pick"}:
        if loc is None and node is None:
            raise RuntimeError(f"unknown select ref {ref}; take a new browser_snapshot")
        pick_suggestion(page, loc, node, str(step.get("text") or step.get("value") or ""))
    elif kind == "check":
        if loc is None:
            raise RuntimeError(f"unknown check ref {ref}")
        loc.check(timeout=12000)
    elif kind == "uncheck":
        if loc is None:
            raise RuntimeError(f"unknown uncheck ref {ref}")
        loc.uncheck(timeout=12000)
    elif kind == "hover":
        if loc is None:
            raise RuntimeError(f"unknown hover ref {ref}")
        loc.hover(timeout=12000)
    elif kind == "drag":
        to_ref = str(step.get("to") or step.get("to_ref") or "")
        to_node = _find(to_ref) if to_ref else None
        to_loc = _locator(page, {"ref": to_ref}, to_node) if to_ref else None
        if loc is not None and to_loc is not None:
            loc.drag_to(to_loc, timeout=12000)
        else:
            x1 = int((node or {}).get("x") or step.get("x") or 0)
            y1 = int((node or {}).get("y") or step.get("y") or 0)
            x2 = int((to_node or {}).get("x") or step.get("x2") or 0)
            y2 = int((to_node or {}).get("y") or step.get("y2") or 0)
            page.mouse.move(x1, y1)
            page.mouse.down()
            page.mouse.move(x2, y2)
            page.mouse.up()
    elif kind in {"press", "key"}:
        page.keyboard.press(str(step.get("text") or step.get("key") or "Enter"))
    elif kind == "scroll":
        page.mouse.wheel(int(step.get("dx") or 0), int(step.get("dy") or 800))
    elif kind == "wait":
        page.wait_for_timeout(int(step.get("wait_ms") or step.get("ms") or 1000))
    elif kind in {"wait_for", "waitfor"}:
        timeout = int(step.get("wait_ms") or step.get("ms") or 8000)
        needle = str(step.get("text") or ref)
        if loc is not None:
            loc.wait_for(state="visible", timeout=timeout)
        elif needle:
            page.get_by_text(needle, exact=False).first.wait_for(state="visible", timeout=timeout)
        else:
            page.wait_for_timeout(timeout)
    elif kind == "upload":
        return _upload_file(page, loc, node, step)
    elif kind == "download":
        return _click_download(page, loc, node, step)
    elif kind == "reload":
        page.reload(wait_until="domcontentloaded")
    elif kind == "back":
        page.go_back(wait_until="domcontentloaded")
    elif kind == "forward":
        page.go_forward(wait_until="domcontentloaded")
    else:
        raise RuntimeError(f"unsupported action {kind}")
    return None


def _desktop(page, actions: list):
    completed = 0
    for step in actions:
        kind = str(step.get("kind") or "").lower()
        if kind in {"click", "move", "pointer"}:
            page.mouse.click(int(step.get("x") or 0), int(step.get("y") or 0))
        elif kind in {"dblclick", "double_click", "doubleclick"}:
            page.mouse.dblclick(int(step.get("x") or 0), int(step.get("y") or 0))
        elif kind in {"rightclick", "contextclick", "context_click"}:
            page.mouse.click(int(step.get("x") or 0), int(step.get("y") or 0), button="right")
        elif kind in {"type", "fill"}:
            page.keyboard.type(str(step.get("text") or ""))
        elif kind in {"key", "press"}:
            page.keyboard.press(str(step.get("key") or step.get("text") or "Enter"))
        elif kind == "scroll":
            if step.get("dx") is not None or step.get("dy") is not None:
                page.mouse.wheel(int(step.get("dx") or 0), int(step.get("dy") or 0))
            else:
                direction = str(step.get("direction") or "down")
                amount = int(step.get("amount") or 3) * 240
                page.mouse.wheel(0, -amount if direction == "up" else amount)
        elif kind == "wait":
            page.wait_for_timeout(int(step.get("ms") or 350))
        else:
            raise RuntimeError(f"unsupported computer action {kind}")
        completed += 1
        page.wait_for_timeout(200)
    data = _dump(page)
    data["completed"] = completed
    return data


def page_is_empty(url: str) -> bool:
    u = (url or "").strip().lower()
    return (
        not u
        or u in {"about:blank", "chrome://newtab/", "chrome://new-tab-page/"}
        or "chromewebdata" in u
        or "chrome-error" in u
    )


def should_paint_desktop(url: str, title: str, body_html: str = "") -> bool:
    """Keep fetched HTML that Playwright left on about:blank. Only paint an empty tab."""
    if (title or "").strip() == "Computer":
        return False
    if not page_is_empty(url or ""):
        return False
    html = body_html or ""
    if "Computer is on" in html:
        return False
    return len(html.strip()) < 80


def _needs_desktop(page) -> bool:
    url = ""
    title = ""
    html = ""
    try:
        url = page.url or ""
        title = page.title() or ""
        html = page.evaluate("() => ((document.body && document.body.innerHTML) || '').slice(0, 400)") or ""
    except Exception:
        return True
    return should_paint_desktop(url, title, html)


def _paint_desktop(page):
    try:
        page.goto(GOOGLE_HOME, wait_until="domcontentloaded", timeout=25000)
        page.wait_for_timeout(250)
        _dump(page)
        return
    except Exception:
        page.set_content(READY_HTML, wait_until="domcontentloaded", timeout=10000)
        page.wait_for_timeout(150)
        Path(LAST).write_text(
            json.dumps({"url": "about:blank", "title": "Computer", "viewport": {"width": VIEW_W, "height": VIEW_H}})
        )


def browser_headed() -> bool:
    """Headed Chromium only when asked for and an X display exists (E2B Desktop sets DISPLAY=:0)."""
    return os.environ.get("CHECKME_BROWSER_HEADED") == "1" and bool(os.environ.get("DISPLAY"))


_DEFAULT_UA_PLATFORM = "X11; Linux x86_64"
_pw = None


def _proxy_option(cfg: dict) -> dict | None:
    """Playwright proxy dict from cfg["proxy"] ({server, username, password, bypass}) or None."""
    proxy = cfg.get("proxy") if isinstance(cfg.get("proxy"), dict) else None
    if not proxy or not proxy.get("server"):
        return None
    out = {"server": str(proxy["server"])}
    for key in ("username", "password", "bypass"):
        if proxy.get(key):
            out[key] = str(proxy[key])
    return out


def launch_options(headed: bool, cfg: dict | None = None, browser_version: str = "") -> dict:
    """Options for a browser that looks like a person's Chrome, not an automation harness.

    - no --enable-automation (no "controlled by automated software" bar, navigator.webdriver stays false);
    - the browser's own user agent (headed) so the UA, client hints (sec-ch-ua) and navigator.platform agree;
      headless only swaps "HeadlessChrome" for "Chrome" with the real version;
    - HTTP/2 and QUIC on, like real Chrome (forcing HTTP/1.1 is a strong bot signal for CDNs);
    - the user's locale and timezone, and optionally an egress proxy (BROWSER_PROXY_URL on the API).
    """
    cfg = cfg or {}
    locale = str(cfg.get("locale") or "en-US")
    args = [
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled",
        "--no-first-run",
        "--no-default-browser-check",
        "--password-store=basic",
        f"--lang={locale}",
    ]
    if cfg.get("http2") is False:
        args += ["--disable-http2", "--disable-quic"]
    if headed:
        args += ["--window-position=0,0", f"--window-size={VIEW_W},{VIEW_H + 100}"]
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    opts: dict = {
        "headless": not headed,
        "viewport": {"width": VIEW_W, "height": VIEW_H},
        "args": args,
        "ignore_default_args": ["--enable-automation"],
        "ignore_https_errors": True,
        "locale": locale,
        "accept_downloads": True,
        "downloads_path": str(DOWNLOAD_DIR),
    }
    if cfg.get("timezone"):
        opts["timezone_id"] = str(cfg["timezone"])
    if cfg.get("channel") in {"chrome", "chrome-beta", "msedge"}:
        opts["channel"] = cfg["channel"]
    proxy = _proxy_option(cfg)
    if proxy:
        opts["proxy"] = proxy
    ua = str(cfg.get("user_agent") or "")
    if not ua and not headed and browser_version:
        major = browser_version.split(".")[0]
        ua = f"Mozilla/5.0 ({_DEFAULT_UA_PLATFORM}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
    if ua:
        opts["user_agent"] = ua
    return opts


def accept_language(locale: str) -> str:
    """'en-IN' -> 'en-IN,en;q=0.9' (what Chrome sends for a one-language profile)."""
    loc = (locale or "en-US").strip()
    base = loc.split("-")[0]
    return loc if base == loc else f"{loc},{base};q=0.9"


# Only fixes a property when it gives automation away; patching values a real Chrome already has is itself a tell.
_HARDEN_JS = """(() => {
  try { if (navigator.webdriver) Object.defineProperty(Navigator.prototype, 'webdriver', {get: () => false}); } catch (e) {}
  try { if (!window.chrome) Object.defineProperty(window, 'chrome', {value: {runtime: {}}, configurable: true}); } catch (e) {}
})();"""


def harden_context(ctx) -> None:
    try:
        ctx.add_init_script(_HARDEN_JS)
    except Exception:
        pass


def _browser_version(pw, cfg: dict) -> str:
    """The real Chromium version, for a headless UA without 'HeadlessChrome' (one short extra launch)."""
    try:
        kw = {"headless": True}
        if cfg.get("channel") in {"chrome", "chrome-beta", "msedge"}:
            kw["channel"] = cfg["channel"]
        b = pw.chromium.launch(**kw)
        version = b.version
        b.close()
        return version
    except Exception:
        return ""


def _ensure_page():
    global _browser_page, _browser_context, _pw
    if _browser_page is not None:
        if _needs_desktop(_browser_page):
            _paint_desktop(_browser_page)
        drain_browser_events()
        return _browser_page
    if _pw is not None and _browser_context is not None:
        pages = list(getattr(_browser_context, "pages", None) or [])
        _browser_page = pages[0] if pages else _browser_context.new_page()
        _register_tab(_browser_page, activate=True)
        if _needs_desktop(_browser_page):
            _paint_desktop(_browser_page)
        drain_browser_events()
        return _browser_page
    from playwright.sync_api import sync_playwright

    fresh = not Path(PROFILE).exists() or not any(Path(PROFILE).iterdir())
    Path(PROFILE).mkdir(parents=True, exist_ok=True)
    if fresh:
        try:
            Path(FRESH_MARK).write_text("1")
        except Exception:
            pass
    prepare_thread_for_sync_playwright()
    _pw = sync_playwright().start()
    headed = browser_headed()
    version = "" if headed else _browser_version(_pw, CONFIG)
    opts = launch_options(headed, CONFIG, version)
    try:
        _browser_context = _pw.chromium.launch_persistent_context(PROFILE, **opts)
    except Exception:
        if "channel" not in opts:
            raise
        opts.pop("channel", None)  # Google Chrome not installed: the bundled Chromium still works
        _browser_context = _pw.chromium.launch_persistent_context(PROFILE, **opts)
    harden_context(_browser_context)
    _browser_page = _browser_context.pages[0] if _browser_context.pages else _browser_context.new_page()
    _register_tab(_browser_page, activate=True)
    # Queue only: these fire while Playwright's driver loop is running (goto, screenshot, first paint).
    _browser_context.on("page", _on_new_page)
    _browser_context.on("download", _on_download)
    if _needs_desktop(_browser_page):
        _paint_desktop(_browser_page)
    drain_browser_events()
    return _browser_page


# Bot checks --------------------------------------------------------------------------------------------------
def _page_state(page) -> tuple[str, str, str, str]:
    try:
        url = page.url or ""
        title = page.title() or ""
        text = page.inner_text("body")[:6000]
    except Exception:
        return "", "", "", ""
    try:
        html = page.evaluate("() => document.documentElement ? document.documentElement.outerHTML.slice(0, 60000) : ''") or ""
    except Exception:
        html = ""
    return url, title, text, html


def _human_pause(page, ms: int) -> None:
    """Idle like a person reading: a couple of small mouse moves and a short wait."""
    try:
        for _ in range(2):
            page.mouse.move(random.randint(200, VIEW_W - 200), random.randint(150, VIEW_H - 150), steps=random.randint(4, 9))
            page.wait_for_timeout(random.randint(120, 260))
        page.wait_for_timeout(max(0, ms))
    except Exception:
        pass


def wait_out_challenge(page, max_s: float | None = None) -> dict | None:
    """Give a passive JS bot check (Cloudflare 'Just a moment', Akamai, AWS WAF...) time to clear, as a person's
    browser would. Returns the block that remains, or None once the real page shows. CAPTCHAs are never
    clicked or solved here: they are returned at once so the user can solve them on the live screen."""
    budget = float(CONFIG.get("challenge_wait_s", 15) if max_s is None else max_s)
    url, title, text, html = _page_state(page)
    block = detect_block(None, url, title, text, html)
    waited = 0.0
    while block and block.get("kind") == "challenge" and waited < budget:
        _human_pause(page, 1000)
        waited += 1.5
        try:
            page.wait_for_load_state("domcontentloaded", timeout=3000)
        except Exception:
            pass
        url, title, text, html = _page_state(page)
        block = detect_block(None, url, title, text, html)
    return block


def _export_state() -> dict:
    """Cookies of this browser profile, so a new computer can start as a returning visitor."""
    _ensure_page()
    try:
        cookies = _browser_context.cookies()
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}
    fresh = Path(FRESH_MARK).exists()
    return {"ok": True, "cookies": cookies, "count": len(cookies), "fresh_profile": fresh}


def _import_state(cmd: dict) -> dict:
    _ensure_page()
    cookies = [c for c in (cmd.get("cookies") or []) if isinstance(c, dict) and c.get("name") and c.get("domain")]
    added = 0
    for i in range(0, len(cookies), 50):
        try:
            _browser_context.add_cookies(cookies[i : i + 50])
            added += len(cookies[i : i + 50])
        except Exception:
            for c in cookies[i : i + 50]:
                try:
                    _browser_context.add_cookies([c])
                    added += 1
                except Exception:
                    continue
    try:
        Path(FRESH_MARK).unlink()
    except Exception:
        pass
    return {"ok": True, "imported": added}


def _remote_fetch(cmd: dict) -> dict:
    """Read one page in a hosted browser (Browserbase/Steel) over CDP. The API creates the session and passes a
    short-lived connect URL; the hosted browser has its own network and fingerprint. Read-only: text + HTML."""
    cdp = str(cmd.get("cdp_url") or "")
    url = str(cmd.get("url") or "")
    if not cdp or not url:
        raise RuntimeError("remote_fetch needs cdp_url and url")
    _ensure_page()
    browser = _pw.chromium.connect_over_cdp(cdp, timeout=30000)
    try:
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        _wait_content(page, int(cmd.get("settle_ms") or 6000))
        block = wait_out_challenge(page, 12)
        purl, title, text, html = _page_state(page)
        try:
            text = page.inner_text("body")[:12000]
            html = page.content()[:400000]
        except Exception:
            pass
        return {"ok": not block, "url": purl or url, "title": title, "text": text, "html": html, "block": block}
    finally:
        try:
            browser.close()
        except Exception:
            pass


def _register_tab(page, activate: bool) -> str:
    global _tab_seq, _active_tab
    for tid, pg in _tabs.items():
        if pg is page:
            return tid
    _tab_seq += 1
    tid = f"t{_tab_seq}"
    _tabs[tid] = page

    def _closed(*_):
        global _active_tab, _browser_page
        _tabs.pop(tid, None)
        _tab_refs.pop(id(page), None)
        if _active_tab == tid:
            _active_tab = next(reversed(_tabs), "") if _tabs else ""
            _browser_page = _tabs.get(_active_tab)

    try:
        page.on("close", _closed)
    except Exception:
        pass
    if activate or not _active_tab:
        _active_tab = tid
    return tid


def _activate(tid: str):
    global _active_tab, _browser_page
    page = _tabs[tid]
    _active_tab = tid
    _browser_page = page
    try:
        page.bring_to_front()
    except Exception:
        pass
    return page


def _tab_list() -> list:
    rows = []
    for tid, pg in list(_tabs.items()):
        try:
            title = pg.title()
        except Exception:
            title = ""
        rows.append({"tab": tid, "url": getattr(pg, "url", ""), "title": title[:120], "active": tid == _active_tab})
    return rows


def _page_for(cmd: dict):
    """The tab a command targets: cmd["tab"] when given (without switching the screen), else the active tab."""
    global _elements, _elements_url
    _ensure_page()
    tid = str(cmd.get("tab") or "").strip() or _active_tab
    if tid not in _tabs:
        raise RuntimeError(f"no tab {tid}; open tabs: {', '.join(_tabs) or 'none'} (use browser_tabs)")
    page = _tabs[tid]
    _elements, _elements_url = _tab_refs.get(id(page), ([], ""))
    return tid, page


def _with_tab(data: dict, tid: str) -> dict:
    data["tab"] = tid
    data["tabs"] = len(_tabs)
    return data


def _tab_command(command: str, cmd: dict) -> dict:
    _ensure_page()
    if command == "tabs":
        return {"ok": True, "active": _active_tab, "tabs": _tab_list()}
    if command == "new_tab":
        if len(_tabs) >= MAX_TABS:
            raise RuntimeError(f"at most {MAX_TABS} tabs; close one with browser_close_tab first")
        page = _browser_context.new_page()
        tid = _register_tab(page, activate=False)
        if not cmd.get("background"):
            _activate(tid)
        url = str(cmd.get("url") or "").strip()
        if url:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(int(cmd.get("wait_ms") or 1200))
            return _with_tab(_snapshot(page), tid)
        return {"ok": True, "tab": tid, "active": _active_tab, "tabs": _tab_list()}
    if command == "switch_tab":
        tid = str(cmd.get("tab") or "").strip()
        if tid not in _tabs:
            raise RuntimeError(f"no tab {tid}; open tabs: {', '.join(_tabs) or 'none'}")
        page = _activate(tid)
        return _with_tab(_snapshot(page), tid)
    if command == "close_tab":
        tid = str(cmd.get("tab") or "").strip() or _active_tab
        if tid not in _tabs:
            raise RuntimeError(f"no tab {tid}; open tabs: {', '.join(_tabs) or 'none'}")
        if len(_tabs) == 1:
            # Keep one tab so the screen and the browser stay alive.
            _tabs[tid].goto("about:blank")
            return {"ok": True, "closed": tid, "note": "last tab cleared instead of closed", "tabs": _tab_list()}
        page = _tabs[tid]
        page.close()
        _tabs.pop(tid, None)
        _tab_refs.pop(id(page), None)
        if _active_tab == tid or _active_tab not in _tabs:
            _activate(next(reversed(_tabs)))
        return {"ok": True, "closed": tid, "active": _active_tab, "tabs": _tab_list()}
    raise RuntimeError(f"unknown tab command {command}")


MAX_TABS = 12
TAB_COMMANDS = {"tabs", "new_tab", "switch_tab", "close_tab"}


def handle(cmd: dict) -> dict:
    try:
        command = str(cmd.get("command") or "").strip()
        if command == "serp":
            return _serp(cmd)
        if command == "export_state":
            return _export_state()
        if command == "import_state":
            return _import_state(cmd)
        if command == "remote_fetch":
            return _remote_fetch(cmd)
        if command in TAB_COMMANDS:
            return _tab_command(command, cmd)
        if command in {"navigate", "goto", "snapshot", "act", "observe", "screenshot", "load_html"}:
            tid, page = _page_for(cmd)
            data = _handle_page(command, cmd, page)
            return _with_tab(data, tid) if isinstance(data, dict) else data
        return _handle_page(command, cmd, _ensure_page())
    finally:
        drain_browser_events()


def _handle_page(command: str, cmd: dict, page) -> dict:
    wait = int(cmd.get("wait_ms") or 1200)
    if command in {"navigate", "goto"}:
        status = None
        try:
            resp = page.goto(cmd["url"], wait_until="domcontentloaded", timeout=int(cmd.get("timeout_ms") or 60000))
            status = resp.status if resp is not None else None
            page.wait_for_timeout(min(wait, 1500))
            challenge = wait_out_challenge(page)
            consent = _dismiss_consent(page)
            _wait_content(page, int(cmd.get("settle_ms") if cmd.get("settle_ms") is not None else 8000))
        except Exception as vis_exc:
            data = _dump(page)
            data["ok"] = False
            data["error"] = str(vis_exc)[:500]
            data["fallback"] = "computer_act"
            return annotate_page(data)
        data = _snapshot(page)
        for _ in range(2):
            if data.get("bot_wall") or not page_is_thin(str(data.get("text") or "")):
                break
            try:
                page.mouse.wheel(0, 900)
                page.wait_for_timeout(3000)
            except Exception:
                break
            data = _snapshot(page)
        if consent:
            data["consent_dismissed"] = consent
        if challenge and not data.get("bot_wall"):
            data["challenge_cleared"] = True
        if status:
            data["status"] = status
            if status in {401, 403, 429, 451, 503} and not data.get("bot_wall"):
                wall = bot_wall(str(data.get("url") or ""), str(data.get("title") or ""), str(data.get("text") or ""), status=status)
                if wall:
                    data.update(wall)
                    data["ok"] = False
                    data["hint"] = f"{wall['reason']} Use the read-only copy if one is attached, else other sites."
        return data
    if command == "snapshot":
        return _snapshot(page)
    if command == "act":
        return _act_page(page, cmd.get("actions") or [])
    if command in {"observe", "screenshot"}:
        return _dump(page)
    if command == "load_html":
        html = str(cmd.get("html") or "")
        target = str(cmd.get("url") or "about:blank")
        page.set_content(html, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(400)
        data = _snapshot(page)
        data["url"] = target
        data["ok"] = True
        Path(LAST).write_text(
            json.dumps({"url": target, "title": data.get("title") or page.title(), "viewport": {"width": VIEW_W, "height": VIEW_H}})
        )
        return data
    if command in {"desktop_act", "control"}:
        return _desktop(page, cmd.get("actions") or [])
    if command == "wake":
        if _needs_desktop(page):
            _paint_desktop(page)
        return _dump(page)
    if command == "status":
        last = {}
        if Path(LAST).exists():
            try:
                last = json.loads(Path(LAST).read_text() or "{}")
            except Exception:
                last = {}
        return {
            "ok": True,
            "url": page.url,
            "title": page.title(),
            "viewport": {"width": VIEW_W, "height": VIEW_H},
            **last,
        }
    raise RuntimeError(f"unknown command {command}")


def _screen_shot():
    try:
        page = _ensure_page()
        if _needs_desktop(page):
            _paint_desktop(page)
        page.screenshot(path=SHOT, full_page=False)
    finally:
        drain_browser_events()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def _send(self, code: int, payload: dict):
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_bytes(self, code: int, body: bytes, content_type: str):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/health"):
            last = {}
            if Path(LAST).exists():
                try:
                    last = json.loads(Path(LAST).read_text() or "{}")
                except Exception:
                    last = {}
            self._send(200, {"ok": True, "version": VERSION, "config": str(CONFIG.get("hash") or ""), **last})
            return
        if self.path.startswith("/screen"):
            try:
                on_browser_thread(_screen_shot)
            except Exception:
                pass
            if Path(SHOT).exists():
                self._send_bytes(200, Path(SHOT).read_bytes(), "image/png")
                return
            self._send(404, {"ok": False, "error": "no screen yet"})
            return
        self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b"{}"
        try:
            cmd = json.loads(body.decode("utf-8"))
        except Exception:
            self._send(400, {"ok": False, "error": "invalid json", "fallback": "computer_act"})
            return
        try:
            result = on_browser_thread(handle, cmd)
            self._send(200, result)
        except Exception as exc:
            self._send(
                200,
                {
                    "ok": False,
                    "error": str(exc)[:500],
                    "fallback": "computer_act",
                    "screenshot_path": SHOT if Path(SHOT).exists() else "",
                    "screenshot_name": "page.png",
                },
            )


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
