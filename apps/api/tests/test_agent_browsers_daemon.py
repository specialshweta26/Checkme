"""The browser daemon with agent browsers, on a real Chrome (docs/AGENT_BROWSERS.md).

Runs only where Playwright is installed (skipped otherwise, e.g. in CI): it starts computer_daemon.py on this machine,
headless, against a small local site that shows which session cookie the browser sent.
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("playwright")

SRC = Path(__file__).resolve().parents[1] / "app" / "connectors"


class _Site(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        who = self.path.split("=", 1)[1] if self.path.startswith("/login?who=") else ""
        body = (
            f"<html><head><title>Cookie site</title></head><body><h1>Signed in as: {self.headers.get('Cookie') or 'nobody'}</h1>"
            "<p>This page shows which session cookie the browser sent, so the test can tell whose browser it is. It has "
            "enough words not to look like an empty page to the block detector.</p></body></html>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        if who:
            self.send_header("Set-Cookie", f"session={who}; Path=/; Max-Age=3600")
        self.end_headers()
        self.wfile.write(body.encode())


def _free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def test_each_agent_browser_has_its_own_sign_ins(tmp_path):
    if not _free(18765):
        pytest.skip("port 18765 is in use")
    run = tmp_path / "run"
    run.mkdir()
    shutil.copy(SRC / "computer_daemon.py", run / "computer_daemon.py")
    shutil.copy(SRC / "block_detect.py", run / "checkme_block_detect.py")
    shutil.copy(SRC / "listing_collect.py", run / "checkme_listing_collect.py")
    HOME = run / "home"
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Site)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    SITE = f"http://127.0.0.1:{srv.server_address[1]}"
    env = {**os.environ, "CHECKME_BROWSER_HOME": str(HOME), "CHECKME_DOWNLOADS": str(run / "dl")}
    log = open(run / "daemon.log", "w")
    proc = subprocess.Popen([sys.executable, "computer_daemon.py"], cwd=run, env=env, stdout=log, stderr=subprocess.STDOUT)

    def rpc(**cmd):
        req = urllib.request.Request("http://127.0.0.1:18765/", data=json.dumps(cmd).encode(), headers={"Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req, timeout=120).read())

    fails: list[str] = []

    def check(name, ok, extra=""):
        if not ok:
            fails.append(f"{name} [{extra}]")

    try:
        for _ in range(60):
            try:
                rpc(command="tabs")
                break
            except Exception:
                time.sleep(0.5)
        # Shared browser signs in as "you".
        r = rpc(command="navigate", url=f"{SITE}/login?who=you")
        check("shared navigate ok", r.get("ok") is not False, r.get("error"))
        shared_tab = r.get("tab")
        r = rpc(command="navigate", url=f"{SITE}/")
        check("shared browser sees your sign-in", "session=you" in (r.get("text") or ""), (r.get("text") or "")[:120])

        # Agent A's own browser: does not see "you".
        r = rpc(command="navigate", url=f"{SITE}/", space="agentA")
        check("agent A starts signed out", "nobody" in (r.get("text") or ""), (r.get("text") or "")[:120])
        check("agent A's first command reports a fresh profile", r.get("space_fresh") is True, r.get("space_fresh"))
        a_tab = r.get("tab")
        check("agent A gets its own tab", a_tab and a_tab != shared_tab, a_tab)
        rpc(command="navigate", url=f"{SITE}/login?who=agentA", space="agentA")
        r = rpc(command="navigate", url=f"{SITE}/", space="agentA")
        check("agent A keeps its own sign-in", "session=agentA" in (r.get("text") or ""), (r.get("text") or "")[:120])
        check("fresh is reported only once", not r.get("space_fresh"), r.get("space_fresh"))

        # Shared browser unaffected.
        r = rpc(command="navigate", url=f"{SITE}/")
        check("shared still you after agent A signed in", "session=you" in (r.get("text") or ""), (r.get("text") or "")[:120])
        check("shared command uses a shared tab", r.get("tab") == shared_tab, r.get("tab"))

        # Isolation of tabs.
        r = rpc(command="snapshot", tab=a_tab)
        check("shared cannot drive agent A's tab", r.get("ok") is False and "no tab" in str(r.get("error")), r)
        r = rpc(command="snapshot", tab=shared_tab, space="agentA")
        check("agent A cannot drive the shared tab", r.get("ok") is False and "no tab" in str(r.get("error")), r)
        r = rpc(command="tabs", space="agentA")
        check("agent A lists only its tabs", [t["tab"] for t in r.get("tabs", [])] == [a_tab], r.get("tabs"))
        r = rpc(command="tabs")
        check("shared lists no agent tabs", a_tab not in [t["tab"] for t in r.get("tabs", [])], r.get("tabs"))

        # New tab in agent A shares A's cookies.
        r = rpc(command="new_tab", url=f"{SITE}/", background=True, space="agentA")
        check("agent A's new tab is signed in as A", "session=agentA" in (r.get("text") or ""), (r.get("text") or "")[:120])
        a_tab2 = r.get("tab")
        r = rpc(command="close_tab", tab=a_tab2, space="agentA")
        check("agent A closes its tab", r.get("closed") == a_tab2, r)

        # Cookies export / import per space.
        r = rpc(command="export_state", space="agentA")
        names = {(c["name"], c["value"]) for c in r.get("cookies", [])}
        check("export agent A cookies", ("session", "agentA") in names and ("session", "you") not in names, names)
        r = rpc(command="export_state")
        names = {(c["name"], c["value"]) for c in r.get("cookies", [])}
        check("export shared cookies", ("session", "you") in names and ("session", "agentA") not in names, names)
        r = rpc(command="import_state", space="agentB", cookies=[{"name": "session", "value": "restoredB", "domain": "127.0.0.1", "path": "/"}])
        check("import into agent B", r.get("imported") == 1, r)
        r = rpc(command="navigate", url=f"{SITE}/", space="agentB")
        check("agent B restored sign-in", "session=restoredB" in (r.get("text") or ""), (r.get("text") or "")[:120])
        check("import cleared B's fresh flag", not r.get("space_fresh"), r.get("space_fresh"))

        # Cap: C and D open, A (least recently used) closes, but its profile keeps the sign-in.
        rpc(command="navigate", url=f"{SITE}/", space="agentC")
        rpc(command="navigate", url=f"{SITE}/", space="agentD")
        r = rpc(command="snapshot", tab=a_tab, space="agentA")
        check("A's old tab is gone after it was closed for room", r.get("ok") is False, r.get("ok"))
        r = rpc(command="navigate", url=f"{SITE}/", space="agentA")
        check("A reopens still signed in (profile on disk)", "session=agentA" in (r.get("text") or ""), (r.get("text") or "")[:120])
        check("profile folders per agent", sorted(p.name for p in (HOME / "spaces").iterdir()) == ["agentA", "agentB", "agentC", "agentD"])

        # Bad names are refused.
        r = rpc(command="tabs", space="../x")
        check("bad space name refused", r.get("ok") is False and "space" in str(r.get("error")), r)
        r = rpc(command="navigate", url=f"{SITE}/")
        check("shared still fine at the end", "session=you" in (r.get("text") or ""), (r.get("text") or "")[:120])
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except Exception:
            proc.kill()
        srv.shutdown()
        log.close()
    assert not fails, "\n".join(fails) + "\n" + (run / "daemon.log").read_text()[-2000:]
