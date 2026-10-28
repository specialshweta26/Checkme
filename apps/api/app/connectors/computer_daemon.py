"""Persistent Chromium on the cloud computer (Lumio page-browser pattern). Headed on E2B Desktop, headless elsewhere."""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = "127.0.0.1"
PORT = 18765
VERSION = "11"
SHOT = "/tmp/checkme-browser.png"
PROFILE = "/tmp/checkme-browser-profile"
LAST = "/tmp/checkme-browser-last.json"
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
_browser_page = None
_browser_context = None
_elements: list[dict] = []

_BOT_URL = (
    "/sorry",
    "recaptcha",
    "hcaptcha",
    "cf-challenge",
    "challenges.cloudflare",
    "geo.captcha",
    "px-captcha",
    "datadome",
)
_BOT_TEXT = (
    "unusual traffic",
    "unusual activity",
    "automated queries",
    "our systems have detected",
    "verify you are human",
    "are you a robot",
    "i'm not a robot",
    "enable javascript and cookies",
    "checking your browser",
    "attention required",
    "please complete the security check",
    "please verify you are",
    "select all images",
)
_SETTLING = ("loading", "please wait", "just a moment", "searching")


def page_is_thin(text: str) -> bool:
    body = (text or "").strip()
    if len(body) < 180:
        return True
    low = body.lower()
    return any(token in low for token in _SETTLING) and len(body) < 1200


def bot_wall(url: str = "", title: str = "", text: str = "") -> dict | None:
    blob = " ".join([url or "", title or "", (text or "")[:4000]]).lower()
    if "before you continue" in blob:
        return None
    if any(token in blob for token in _BOT_URL) or any(token in blob for token in _BOT_TEXT):
        kind = "captcha" if "robot" in blob or "select all images" in blob else "verification"
        return {
            "bot_wall": True,
            "bot_wall_kind": kind,
            "handoff": True,
            "reason": "This site is blocking the cloud browser (CAPTCHA or verification).",
        }
    return None


def annotate_page(data: dict | None) -> dict:
    row = data if isinstance(data, dict) else {}
    wall = bot_wall(str(row.get("url") or ""), str(row.get("title") or ""), str(row.get("text") or ""))
    if wall:
        row.update(wall)
        row["ok"] = False
        row["content_ready"] = False
        row["hint"] = (
            "Do not invent results. The user must finish verification on this screen "
            "(request_takeover / browser_handoff). After they finish, snapshot and continue."
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


def _snapshot(page):
    nodes = page.evaluate(
        """() => {
          const sel = 'a, button, input, textarea, select, option, label, summary, [role="button"], [role="link"], [role="textbox"], [role="checkbox"], [role="radio"], [role="combobox"], [role="option"], [role="tab"], [contenteditable="true"]';
          return Array.from(document.querySelectorAll(sel)).slice(0, 120).map((el, i) => {
            const name = (el.getAttribute('aria-label') || el.getAttribute('name') || el.innerText || el.getAttribute('placeholder') || el.value || el.getAttribute('title') || '').replace(/\\s+/g, ' ').trim().slice(0, 80);
            const tags = {A: 'link', BUTTON: 'button', INPUT: 'textbox', TEXTAREA: 'textbox', SELECT: 'combobox', OPTION: 'option', LABEL: 'label', SUMMARY: 'button'};
            const r = el.getBoundingClientRect();
            return {
              ref: 'e' + i,
              role: (el.getAttribute('role') || tags[el.tagName] || el.tagName.toLowerCase()),
              name,
              tag: el.tagName.toLowerCase(),
              href: el.href || '',
              input_type: el.getAttribute('type') || '',
              x: Math.round(r.x + r.width / 2),
              y: Math.round(r.y + r.height / 2),
            };
          });
        }"""
    )
    global _elements
    _elements = nodes if isinstance(nodes, list) else []
    tree = "\n".join(f"- {n.get('ref')} {n.get('role')} {n.get('name') or ''}".strip() for n in _elements)
    data = _dump(page)
    data["tree"] = tree
    data["elements"] = _elements
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


def _locator(page, step, node):
    ref = str(step.get("ref") or step.get("selector") or "").strip()
    if ref and not _find(ref):
        for factory in (
            lambda: page.locator(ref).first,
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
    if node and node.get("name"):
        role = str(node.get("role") or "generic")
        for factory in (
            lambda: page.get_by_role(role, name=node["name"]).first,
            lambda: page.get_by_label(node["name"], exact=False).first,
            lambda: page.get_by_placeholder(node["name"], exact=False).first,
            lambda: page.get_by_text(node["name"], exact=False).first,
        ):
            try:
                loc = factory()
            except Exception:
                loc = None
            if _has(loc):
                return loc
    return None


def _act_page(page, actions: list):
    completed = 0
    for step in actions:
        kind = str(step.get("kind") or "").lower()
        ref = str(step.get("ref") or step.get("selector") or "").strip()
        node = _find(ref) if ref else None
        loc = _locator(page, step, node)
        if kind == "click":
            if loc is not None:
                loc.click(timeout=12000)
            elif step.get("x") is not None:
                page.mouse.click(int(step["x"]), int(step["y"]))
            elif node and node.get("x") is not None:
                page.mouse.click(int(node["x"]), int(node["y"]))
            else:
                raise RuntimeError(f"unknown ref {ref}")
        elif kind in {"fill", "type"}:
            text = str(step.get("text") or "")
            if loc is not None:
                loc.fill(text, timeout=12000) if kind == "fill" else loc.type(text, timeout=12000)
            else:
                page.keyboard.type(text)
        elif kind in {"select", "select_option"}:
            value = str(step.get("text") or step.get("value") or "")
            if loc is None:
                raise RuntimeError(f"unknown select ref {ref}")
            loc.select_option(value, timeout=12000)
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
        elif kind in {"press", "key"}:
            page.keyboard.press(str(step.get("text") or step.get("key") or "Enter"))
        elif kind == "scroll":
            page.mouse.wheel(int(step.get("dx") or 0), int(step.get("dy") or 800))
        elif kind == "wait":
            page.wait_for_timeout(int(step.get("wait_ms") or step.get("ms") or 1000))
        elif kind == "reload":
            page.reload(wait_until="domcontentloaded")
        elif kind == "back":
            page.go_back(wait_until="domcontentloaded")
        else:
            raise RuntimeError(f"unsupported action {kind}")
        completed += 1
        page.wait_for_timeout(250)
    data = _snapshot(page)
    data["completed"] = completed
    return data


def _desktop(page, actions: list):
    completed = 0
    for step in actions:
        kind = str(step.get("kind") or "").lower()
        if kind in {"click", "move", "pointer"}:
            page.mouse.click(int(step.get("x") or 0), int(step.get("y") or 0))
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


def _ensure_page():
    global _browser_page, _browser_context
    if _browser_page is not None:
        if _needs_desktop(_browser_page):
            _paint_desktop(_browser_page)
        return _browser_page
    from playwright.sync_api import sync_playwright

    Path(PROFILE).mkdir(parents=True, exist_ok=True)
    pw = sync_playwright().start()
    headed = browser_headed()
    extra = ["--window-position=0,0", f"--window-size={VIEW_W},{VIEW_H + 100}", "--no-first-run", "--no-default-browser-check"] if headed else []
    _browser_context = pw.chromium.launch_persistent_context(
        PROFILE,
        # On a real desktop (E2B) Chromium runs headed so it shows in the live stream; elsewhere it stays headless.
        headless=not headed,
        viewport={"width": VIEW_W, "height": VIEW_H},
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--disable-http2",
            "--disable-quic",
            "--ignore-certificate-errors",
            *extra,
        ],
    )
    _browser_page = _browser_context.pages[0] if _browser_context.pages else _browser_context.new_page()
    if _needs_desktop(_browser_page):
        _paint_desktop(_browser_page)
    return _browser_page


def handle(cmd: dict) -> dict:
    command = str(cmd.get("command") or "").strip()
    page = _ensure_page()
    wait = int(cmd.get("wait_ms") or 1200)
    if command in {"navigate", "goto"}:
        try:
            page.goto(cmd["url"], wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(wait)
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
            self._send(200, {"ok": True, "version": VERSION, **last})
            return
        if self.path.startswith("/screen"):
            try:
                with _lock:
                    page = _ensure_page()
                    if _needs_desktop(page):
                        _paint_desktop(page)
                    page.screenshot(path=SHOT, full_page=False)
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
            with _lock:
                result = handle(cmd)
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
