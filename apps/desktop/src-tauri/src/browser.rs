use std::path::PathBuf;
use std::process::Stdio;
use std::time::Duration;

use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use tokio::process::{Child, Command};
use tokio_tungstenite::connect_async;
use tokio_tungstenite::tungstenite::Message;

const SNAPSHOT_JS: &str = include_str!("snapshot.js");
const COLLECT_JS: &str = include_str!("collect.js");
const ACT_JS: &str = include_str!("act.js");
const PICKER_JS: &str = include_str!("picker.js");
const WATCH_JS: &str = include_str!("watch.js");
const DRAIN_DIALOGS_JS: &str = "window.__lumioWatch ? window.__lumioWatch.drain() : []";
/// A pop-up raised while a page loaded, closed because it blocked the page. Reported with the next page read.
static DISMISSED_DIALOG: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);
const TEACH_JS: &str = include_str!("teach.js");
const DRAIN_TEACH_JS: &str = r#"(() => { const b = window.__lumioTeachBuf || []; window.__lumioTeachBuf = []; return b.slice(0, 200); })()"#;
const SUBMIT_IF_DRAFT_JS: &str = r#"(() => {
  const el = document.activeElement;
  const draft = !el ? '' : (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') ? String(el.value || '') : (el.isContentEditable ? String(el.innerText || '').trim() : '');
  if (!draft) return { draft_cleared: true };
  const hit = Array.from(document.querySelectorAll('button, [role="button"]')).find((node) => {
    const r = node.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    const st = getComputedStyle(node);
    if (st.visibility === 'hidden' || st.display === 'none') return false;
    const name = (node.getAttribute('aria-label') || node.getAttribute('title') || node.innerText || '').replace(/\s+/g, ' ').trim();
    return /^(send|submit)\b/i.test(name);
  });
  if (!hit) return { draft_cleared: false };
  hit.click();
  return { clicked_send: true };
})()"#;

const MAX_ELEMENTS: usize = 150;
// What a click is expected to change (address, title, page size and text, the focused field, open menus and dialogs,
// checked / selected controls, scroll). The same before and after means the click did nothing visible.
const PAGE_SIG_JS: &str = r#"(() => {
  const a = document.activeElement;
  const field = a && (a.tagName === 'INPUT' || a.tagName === 'TEXTAREA' || a.tagName === 'SELECT' || a.isContentEditable);
  const count = (sel) => document.querySelectorAll(sel).length;
  return [
    location.href, document.title, document.getElementsByTagName('*').length,
    ((document.body && document.body.innerText) || '').length,
    field ? (a.tagName + '#' + (a.id || a.getAttribute('name') || '')) : '',
    count('[role="dialog"], [role="menu"], [role="listbox"], [aria-expanded="true"], [open]'),
    count('input:checked, option:checked, [aria-selected="true"], [aria-pressed="true"], [aria-checked="true"]'),
    Math.round(window.scrollY),
  ].join('|');
})()"#;
// Resolves when the DOM has been quiet for a moment and no loading indicator (spinner, progress bar, aria-busy)
// shows, or at the caps. Replaces fixed sleeps after an action.
const SETTLE_JS: &str = r#"new Promise((resolve) => {
  const BUSY = '[aria-busy="true"], [role="progressbar"], .spinner, .loading, .loader, .spinner-border, .MuiCircularProgress-root, .ant-spin-spinning, [class*="spinner"], [data-loading="true"]';
  const busy = () => {
    for (const el of document.querySelectorAll(BUSY)) {
      const r = el.getBoundingClientRect();
      if (r.width < 4 || r.height < 4 || r.bottom < 0 || r.top > window.innerHeight) continue;
      const st = getComputedStyle(el);
      if (st.visibility === 'hidden' || st.display === 'none' || Number(st.opacity || 1) < 0.05) continue;
      const now = el.getAttribute('aria-valuenow');
      if (now !== null && Number(now) >= Number(el.getAttribute('aria-valuemax') || 100)) continue;
      return true;
    }
    return false;
  };
  const started = Date.now();
  let last = Date.now();
  let obs = null;
  try {
    obs = new MutationObserver(() => { last = Date.now(); });
    obs.observe(document.documentElement || document, { subtree: true, childList: true, attributes: true, characterData: true });
  } catch (e) {}
  const tick = () => {
    const quiet = Date.now() - last >= 250;
    const waited = Date.now() - started;
    if ((quiet && !busy()) || waited > 8000 || (quiet && waited > 2500 && !busy())) {
      try { obs && obs.disconnect(); } catch (e) {}
      resolve(waited);
      return;
    }
    setTimeout(tick, 100);
  };
  setTimeout(tick, 120);
})"#;
const MAX_FRAMES: usize = 8;
const MAX_FRAME_TEXT: usize = 3000;
// Enough text, a visible field (sign-in and OTP pages are short), or a loaded frame that may hold the form.
const PAGE_READY_JS: &str = r#"(() => {
  const body = document.body;
  if ((body && body.innerText || '').replace(/\s+/g, ' ').trim().length > 180) return true;
  const shows = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const hasField = (root) => {
    for (const el of root.querySelectorAll('*')) {
      if (el.matches('input:not([type=hidden]), textarea, select, [contenteditable="true"]') && shows(el)) return true;
      if (el.shadowRoot && hasField(el.shadowRoot)) return true;
    }
    return false;
  };
  if (hasField(document)) return true;
  if (document.readyState !== 'complete') return false;
  return Array.from(document.querySelectorAll('iframe')).some((f) => { const r = f.getBoundingClientRect(); return r.width >= 200 && r.height >= 100; });
})()"#;

/// The other-site frames of the page at `page_ws`, from Chrome's target list.
fn frames_of_page(targets: &[Value], page_ws: &str) -> Vec<String> {
    let Some(page_id) = targets
        .iter()
        .find(|t| t.get("webSocketDebuggerUrl").and_then(Value::as_str) == Some(page_ws))
        .and_then(|t| t.get("id").and_then(Value::as_str))
    else {
        return vec![];
    };
    targets
        .iter()
        .filter(|t| t.get("type").and_then(Value::as_str) == Some("iframe"))
        .filter(|t| t.get("parentId").and_then(Value::as_str) == Some(page_id))
        .filter_map(|t| t.get("webSocketDebuggerUrl").and_then(Value::as_str).map(str::to_string))
        .take(MAX_FRAMES)
        .collect()
}

/// Add one other-site frame's snapshot to the page's. Its x/y are relative to the frame, so they're dropped: its
/// elements are acted on by ref. Frames with nothing to act on or read (trackers, empty ads) are left out.
fn merge_frame(page: &mut Value, frame: &Value) {
    let elements = frame.get("elements").and_then(Value::as_array).cloned().unwrap_or_default();
    let text = frame.get("text").and_then(Value::as_str).unwrap_or("").trim();
    if elements.is_empty() && text.len() < 20 {
        return;
    }
    let url = frame.get("url").and_then(Value::as_str).unwrap_or("");
    if let Some(list) = page.get_mut("elements").and_then(Value::as_array_mut) {
        list.extend(elements.into_iter().map(|mut e| {
            if let Some(row) = e.as_object_mut() {
                row.remove("x");
                row.remove("y");
                row.insert("frame".into(), json!(url));
            }
            e
        }));
    }
    let tree = frame.get("tree").and_then(Value::as_str).unwrap_or("");
    if !tree.is_empty() {
        let joined = format!("{}\n[frame {url}]\n{tree}", page["tree"].as_str().unwrap_or(""));
        page["tree"] = json!(joined.trim_start());
    }
    if !text.is_empty() {
        let clipped: String = text.chars().take(MAX_FRAME_TEXT).collect();
        let joined = format!("{}\n[frame {url}]\n{clipped}", page["text"].as_str().unwrap_or(""));
        page["text"] = json!(joined.trim_start());
    }
    // The focused field may be in the frame.
    if page["composer"].as_str().unwrap_or("").is_empty() {
        if let Some(composer) = frame.get("composer").filter(|c| c.as_str().is_some_and(|t| !t.is_empty())) {
            page["composer"] = composer.clone();
        }
    }
}

/// Tell the agent the element list was cut, and how to reach the rest.
fn note_omitted(page: &mut Value, total: usize) {
    let shown = page.get("elements").and_then(Value::as_array).map_or(0, Vec::len);
    if total <= shown {
        return;
    }
    let omitted = total - shown;
    let line = format!(
        "… {omitted} more elements are not listed. Scroll to them, or call browser_snapshot with find=\"words\" (for example the menu item or report name) to list the matching ones first."
    );
    let tree = format!("{}\n{line}", page["tree"].as_str().unwrap_or(""));
    page["tree"] = json!(tree.trim_start());
    page["elements_omitted"] = json!(omitted);
}

fn no_effect_note(index: usize, kind: &str, ref_id: &str) -> String {
    let what = format!("{kind} {ref_id}");
    format!(
        "step {} ({}) may not have done anything: the page looks the same as before. Check the page; if nothing opened or changed, try another element (its label or parent), hover first, or use computer_act at its x,y.",
        index + 1,
        what.trim()
    )
}

/// True at most once every few seconds.
fn shot_due() -> bool {
    static LAST: std::sync::OnceLock<std::sync::Mutex<Option<std::time::Instant>>> = std::sync::OnceLock::new();
    let Ok(mut last) = LAST.get_or_init(|| std::sync::Mutex::new(None)).lock() else {
        return true;
    };
    if last.is_some_and(|at| at.elapsed() < Duration::from_secs(3)) {
        return false;
    }
    *last = Some(std::time::Instant::now());
    true
}

pub struct Chrome {
    _child: Option<Child>,
    port: u16,
    http: reqwest::Client,
    /// The tab this job works on (from its `tab` argument); jobs run one at a time.
    tab: std::sync::Mutex<Option<String>>,
}

fn chrome_candidates() -> Vec<PathBuf> {
    let mut out = Vec::new();
    if cfg!(windows) {
        if let Ok(local) = std::env::var("LOCALAPPDATA") {
            out.push(PathBuf::from(local).join(r"Google\Chrome\Application\chrome.exe"));
        }
        out.push(PathBuf::from(r"C:\Program Files\Google\Chrome\Application\chrome.exe"));
        out.push(PathBuf::from(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"));
        out.push(PathBuf::from(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"));
    } else {
        out.push(PathBuf::from("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"));
        out.push(PathBuf::from("/Applications/Chromium.app/Contents/MacOS/Chromium"));
        out.push(PathBuf::from("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"));
        out.push(PathBuf::from("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"));
        out.push(PathBuf::from("/usr/bin/google-chrome"));
    }
    out.into_iter().filter(|p| p.exists()).collect()
}

fn profile_dir() -> PathBuf {
    crate::grants::browser_profile_dir()
}

fn http_client(timeout: Duration) -> Result<reqwest::Client, String> {
    reqwest::Client::builder().timeout(timeout).build().map_err(|e| e.to_string())
}

async fn port_open(http: &reqwest::Client, port: u16) -> bool {
    let url = format!("http://127.0.0.1:{port}/json/version");
    http.get(&url).send().await.map(|r| r.status().is_success()).unwrap_or(false)
}

async fn wait_debug(http: &reqwest::Client, port: u16) -> Result<(), String> {
    let deadline = tokio::time::Instant::now() + Duration::from_secs(8);
    while tokio::time::Instant::now() < deadline {
        if port_open(http, port).await {
            return Ok(());
        }
        tokio::time::sleep(Duration::from_millis(200)).await;
    }
    Err("Chrome did not open its debug port".into())
}

pub async fn attach() -> Result<Chrome, String> {
    let probe = http_client(Duration::from_millis(500))?;
    let port = 9333u16;
    if !port_open(&probe, port).await {
        return Err("Chrome is not open".into());
    }
    let http = http_client(Duration::from_secs(3))?;
    Ok(Chrome { _child: None, port, http, tab: std::sync::Mutex::new(None) })
}

pub async fn launch(start_url: &str) -> Result<Chrome, String> {
    if !(start_url.starts_with("https://") || start_url.starts_with("http://")) {
        return Err("A full https address is required before opening Chrome.".into());
    }
    let probe = http_client(Duration::from_millis(500))?;
    // Dedicated port + Lumio profile. Never attach to 9222 (that is often the user's own Chrome).
    let port = 9333u16;
    let http = http_client(Duration::from_secs(3))?;
    if port_open(&probe, port).await {
        // Attached to a running Chrome: the job's own navigation picks the page. Opening one here would add a tab per
        // navigation, and page_ws opens one if every tab has been closed.
        return Ok(Chrome { _child: None, port, http, tab: std::sync::Mutex::new(None) });
    }
    let bin = chrome_candidates()
        .into_iter()
        .next()
        .ok_or_else(|| "Install Google Chrome (or Edge) to use this computer's browser.".to_string())?;
    let mut cmd = Command::new(&bin);
    cmd.arg(format!("--remote-debugging-port={port}"))
        .arg(format!("--user-data-dir={}", profile_dir().display()))
        .arg("--no-first-run")
        .arg("--no-default-browser-check")
        .arg("--disable-sync")
        .arg("--disable-blink-features=AutomationControlled")
        .arg("--window-size=1280,800")
        .arg(start_url)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .kill_on_drop(false);
    let mut child = cmd.spawn().map_err(|e| format!("could not start Chrome: {e}"))?;
    if let Err(err) = wait_debug(&probe, port).await {
        child.kill().await.ok();
        return Err(err);
    }
    Ok(Chrome { _child: Some(child), port, http, tab: std::sync::Mutex::new(None) })
}

impl Chrome {
    async fn pages(&self) -> Result<Vec<Value>, String> {
        let url = format!("http://127.0.0.1:{}/json/list", self.port);
        let res = self.http.get(&url).send().await.map_err(|e| e.to_string())?;
        res.json().await.map_err(|e| e.to_string())
    }

    pub fn use_tab(&self, tab: Option<&str>) {
        if let Ok(mut current) = self.tab.lock() {
            *current = tab.filter(|id| !id.is_empty()).map(str::to_string);
        }
    }

    pub async fn tabs(&self) -> Result<Value, String> {
        let pages = self.pages().await?;
        let rows: Vec<Value> = pages
            .iter()
            .filter(|p| p.get("type").and_then(Value::as_str) == Some("page"))
            .map(|p| {
                json!({
                    "tab": p.get("id").and_then(Value::as_str).unwrap_or(""),
                    "url": p.get("url").and_then(Value::as_str).unwrap_or(""),
                    "title": p.get("title").and_then(Value::as_str).unwrap_or(""),
                })
            })
            .collect();
        Ok(json!({ "ok": true, "tabs": rows, "ran_on": "local_computer" }))
    }

    pub async fn new_tab(&self, url: &str) -> Result<Value, String> {
        let target = if url.is_empty() { "about:blank" } else { url };
        let res = self
            .http
            .put(format!("http://127.0.0.1:{}/json/new?{target}", self.port))
            .send()
            .await
            .map_err(|e| e.to_string())?;
        let page: Value = res.json().await.map_err(|e| e.to_string())?;
        let id = page.get("id").and_then(Value::as_str).ok_or_else(|| "Chrome did not open a tab".to_string())?.to_string();
        self.use_tab(Some(&id));
        let mut data = self.snapshot().await?;
        data["tab"] = json!(id);
        Ok(data)
    }

    pub async fn switch_tab(&self, id: &str) -> Result<Value, String> {
        let _ = self.http.get(format!("http://127.0.0.1:{}/json/activate/{id}", self.port)).send().await;
        self.use_tab(Some(id));
        let mut data = self.snapshot().await?;
        data["tab"] = json!(id);
        Ok(data)
    }

    pub async fn close_tab(&self, id: &str) -> Result<Value, String> {
        let _ = self.http.get(format!("http://127.0.0.1:{}/json/close/{id}", self.port)).send().await;
        self.use_tab(None);
        self.tabs().await
    }

    async fn page_ws(&self) -> Result<String, String> {
        let wanted = self.tab.lock().ok().and_then(|current| current.clone());
        if let Some(id) = wanted {
            let pages = self.pages().await?;
            let found = pages.iter().find(|p| {
                p.get("type").and_then(Value::as_str) == Some("page") && p.get("id").and_then(Value::as_str) == Some(id.as_str())
            });
            return match found.and_then(|p| p.get("webSocketDebuggerUrl")).and_then(Value::as_str) {
                Some(ws) => Ok(ws.to_string()),
                None => Err(format!("no tab {id}; it has been closed")),
            };
        }
        let mut pages = self.pages().await?;
        if !pages.iter().any(|p| p.get("type").and_then(Value::as_str) == Some("page")) {
            // Every tab is closed (for one, after an app update): open one instead of failing the task.
            let url = format!("http://127.0.0.1:{}/json/new?about:blank", self.port);
            let _ = self.http.put(&url).send().await;
            pages = self.pages().await?;
        }
        let mut blank = None;
        for page in &pages {
            if page.get("type").and_then(Value::as_str) != Some("page") {
                continue;
            }
            let Some(ws) = page.get("webSocketDebuggerUrl").and_then(Value::as_str) else { continue };
            let url = page.get("url").and_then(Value::as_str).unwrap_or("");
            if url.starts_with("http://") || url.starts_with("https://") {
                return Ok(ws.to_string());
            }
            if blank.is_none() {
                blank = Some(ws.to_string());
            }
        }
        blank.ok_or_else(|| "Chrome has no page open. Navigate to an https address first.".to_string())
    }

    async fn cdp(&self, method: &str, params: Value) -> Result<Value, String> {
        self.cdp_wait(method, params, Duration::from_secs(8)).await
    }

    async fn cdp_wait(&self, method: &str, params: Value, wait: Duration) -> Result<Value, String> {
        let ws_url = self.page_ws().await?;
        self.cdp_on(&ws_url, method, params, wait).await
    }

    /// Debug addresses of the open page's frames from other sites. Chrome runs these as separate targets, so script in
    /// the page can't reach them.
    async fn frame_targets(&self) -> Vec<String> {
        let Ok(page_ws) = self.page_ws().await else { return vec![] };
        let Ok(targets) = self.pages().await else { return vec![] };
        frames_of_page(&targets, &page_ws)
    }

    async fn eval_on(&self, ws_url: &str, expression: &str) -> Result<Value, String> {
        let result = self
            .cdp_on(
                ws_url,
                "Runtime.evaluate",
                json!({ "expression": expression, "returnByValue": true, "awaitPromise": false }),
                Duration::from_secs(8),
            )
            .await?;
        Ok(result.pointer("/result/value").cloned().unwrap_or(Value::Null))
    }

    /// Closes a pop-up that is blocking the page. Declining is the safe answer: a page's "OK" can confirm something.
    async fn dismiss_dialog(&self) -> bool {
        let closed = self.cdp("Page.handleJavaScriptDialog", json!({ "accept": false })).await.is_ok();
        if closed {
            DISMISSED_DIALOG.store(true, std::sync::atomic::Ordering::Relaxed);
        }
        closed
    }

    pub async fn page_log(&self) -> Result<Value, String> {
        self.eval(WATCH_JS).await?;
        let mut data = self.eval("window.__lumioWatch.log()").await?;
        data["ok"] = json!(true);
        data["ran_on"] = json!("local_computer");
        Ok(data)
    }

    /// Attach a file to a page's file input. Node ids only live for one connection, so all three calls share one.
    async fn upload_on(&self, ws_url: &str, ref_id: &str, path: &str) -> Result<(), String> {
        let (mut ws, _) = connect_async(ws_url).await.map_err(|e| e.to_string())?;
        let mut next_id: u64 = 0;
        macro_rules! call {
            ($method:expr, $params:expr) => {{
                next_id += 1;
                let id = next_id;
                let payload = json!({ "id": id, "method": $method, "params": $params });
                ws.send(Message::Text(payload.to_string().into())).await.map_err(|e| e.to_string())?;
                let deadline = tokio::time::Instant::now() + Duration::from_secs(8);
                loop {
                    let left = deadline.saturating_duration_since(tokio::time::Instant::now());
                    let msg = tokio::time::timeout(left, ws.next())
                        .await
                        .map_err(|_| "Chrome did not answer".to_string())?
                        .ok_or_else(|| "Chrome closed".to_string())?
                        .map_err(|e| e.to_string())?;
                    let Message::Text(text) = msg else { continue };
                    let v: Value = serde_json::from_str(text.as_str()).map_err(|e| e.to_string())?;
                    if v.get("id") != Some(&json!(id)) {
                        continue;
                    }
                    break match v.get("error") {
                        Some(err) => Err(err.to_string()),
                        None => Ok(v.get("result").cloned().unwrap_or(json!({}))),
                    };
                }
            }};
        }
        let doc: Result<Value, String> = call!("DOM.getDocument", json!({ "depth": 0 }));
        let root = doc?.pointer("/root/nodeId").and_then(Value::as_i64).unwrap_or(0);
        // The ref is usually the input itself; a styled upload button hides the real one elsewhere on the page.
        for selector in [format!("[data-checkme-ref=\"{ref_id}\"]"), "input[type=file]".to_string()] {
            let found: Result<Value, String> = call!("DOM.querySelector", json!({ "nodeId": root, "selector": selector }));
            let node = found?.get("nodeId").and_then(Value::as_i64).unwrap_or(0);
            if node == 0 {
                continue;
            }
            let set: Result<Value, String> = call!("DOM.setFileInputFiles", json!({ "nodeId": node, "files": [path] }));
            if set.is_ok() {
                return Ok(());
            }
        }
        Err("no file input on this page to attach the file to; click the upload button and look at what opens".into())
    }

    async fn mouse_right(&self, target: &str, x: i64, y: i64) -> Result<(), String> {
        let wait = Duration::from_secs(8);
        self.cdp_on(target, "Input.dispatchMouseEvent", json!({ "type": "mouseMoved", "x": x, "y": y }), wait).await?;
        for phase in ["mousePressed", "mouseReleased"] {
            self.cdp_on(
                target,
                "Input.dispatchMouseEvent",
                json!({ "type": phase, "x": x, "y": y, "button": "right", "clickCount": 1 }),
                wait,
            )
            .await?;
        }
        Ok(())
    }

    /// Press on one point, move to another in a few steps (drag handlers listen for movement), and let go.
    async fn drag_between(&self, target: &str, from: (i64, i64), to: (i64, i64)) -> Result<(), String> {
        let wait = Duration::from_secs(8);
        self.cdp_on(target, "Input.dispatchMouseEvent", json!({ "type": "mouseMoved", "x": from.0, "y": from.1 }), wait).await?;
        self.cdp_on(
            target,
            "Input.dispatchMouseEvent",
            json!({ "type": "mousePressed", "x": from.0, "y": from.1, "button": "left", "clickCount": 1 }),
            wait,
        )
        .await?;
        for part in 1..=5i64 {
            let (x, y) = (from.0 + (to.0 - from.0) * part / 5, from.1 + (to.1 - from.1) * part / 5);
            self.cdp_on(target, "Input.dispatchMouseEvent", json!({ "type": "mouseMoved", "x": x, "y": y, "button": "left", "buttons": 1 }), wait).await?;
            tokio::time::sleep(Duration::from_millis(40)).await;
        }
        self.cdp_on(
            target,
            "Input.dispatchMouseEvent",
            json!({ "type": "mouseReleased", "x": to.0, "y": to.1, "button": "left", "clickCount": 1 }),
            wait,
        )
        .await?;
        Ok(())
    }

    /// Where a ref sits on screen, scrolled into view.
    async fn point_of(&self, target: &str, ref_id: &str) -> Result<(i64, i64), String> {
        let found = self.eval_on(target, &format!("({ACT_JS})({ref_id:?}, \"hover\", \"\", true)")).await?;
        let at = found.get("mouse");
        match (at.and_then(|m| m.get("x")).and_then(Value::as_i64), at.and_then(|m| m.get("y")).and_then(Value::as_i64)) {
            (Some(x), Some(y)) => Ok((x, y)),
            _ => Err(format!("{ref_id} can't be reached with the mouse; take a snapshot and try another element")),
        }
    }

    async fn cdp_on(&self, ws_url: &str, method: &str, params: Value, wait: Duration) -> Result<Value, String> {
        let (mut ws, _) = connect_async(ws_url).await.map_err(|e| e.to_string())?;
        let payload = json!({ "id": 1, "method": method, "params": params });
        ws.send(Message::Text(payload.to_string().into())).await.map_err(|e| e.to_string())?;
        let deadline = tokio::time::Instant::now() + wait;
        loop {
            let left = deadline.saturating_duration_since(tokio::time::Instant::now());
            if left.is_zero() {
                return Err("Chrome did not answer".into());
            }
            let msg = tokio::time::timeout(left, ws.next())
                .await
                .map_err(|_| "Chrome did not answer".to_string())?
                .ok_or_else(|| "Chrome closed".to_string())?
                .map_err(|e| e.to_string())?;
            let Message::Text(text) = msg else { continue };
            let v: Value = serde_json::from_str(text.as_str()).map_err(|e| e.to_string())?;
            if v.get("id") != Some(&json!(1)) {
                continue;
            }
            if let Some(err) = v.get("error") {
                return Err(err.to_string());
            }
            return Ok(v.get("result").cloned().unwrap_or(json!({})));
        }
    }

    async fn eval(&self, expression: &str) -> Result<Value, String> {
        self.eval_wait(expression, Duration::from_secs(8)).await
    }

    async fn eval_wait(&self, expression: &str, wait: Duration) -> Result<Value, String> {
        let params = json!({ "expression": expression, "returnByValue": true, "awaitPromise": false });
        let result = match self.cdp_wait("Runtime.evaluate", params.clone(), wait).await {
            Ok(result) => result,
            Err(err) => {
                // A pop-up freezes the page's scripts: close it and read again.
                if err != "Chrome did not answer" || !self.dismiss_dialog().await {
                    return Err(err);
                }
                self.cdp_wait("Runtime.evaluate", params, wait).await?
            }
        };
        if let Some(ex) = result.get("exceptionDetails") {
            // A script that threw used to come back as an empty page, so the agent re-opened a page that was fine.
            // Report what failed instead.
            let detail = ex
                .pointer("/exception/description")
                .and_then(Value::as_str)
                .or_else(|| ex.get("text").and_then(Value::as_str))
                .unwrap_or("the page script failed");
            let first_line: String = detail.lines().next().unwrap_or("").chars().take(300).collect();
            return Err(format!("page script failed: {first_line}"));
        }
        Ok(result.pointer("/result/value").cloned().unwrap_or(Value::Null))
    }

    async fn prepare_page(&self) {
        const HIDE_WEBDRIVER: &str = r#"(() => { try { Object.defineProperty(Navigator.prototype, 'webdriver', { get: () => undefined }); } catch (e) {} })();"#;
        self.cdp("Page.enable", json!({})).await.ok();
        self.cdp(
            "Page.addScriptToEvaluateOnNewDocument",
            json!({ "source": HIDE_WEBDRIVER }),
        )
        .await
        .ok();
    }

    async fn wait_for_page(&self, wait_ms: u64) {
        let budget = wait_ms.max(1500).min(6000);
        let deadline = tokio::time::Instant::now() + Duration::from_millis(budget);
        loop {
            tokio::time::sleep(Duration::from_millis(500)).await;
            let ready = self
                .eval(PAGE_READY_JS)
                .await
                .ok()
                .and_then(|v| v.as_bool())
                .unwrap_or(false);
            if ready || tokio::time::Instant::now() >= deadline {
                break;
            }
        }
    }

    pub async fn navigate(&self, url: &str, wait_ms: u64) -> Result<Value, String> {
        let target = url.trim();
        if !(target.starts_with("https://") || target.starts_with("http://")) {
            return Err("A full https address is required. Refusing to open a blank page.".into());
        }
        self.prepare_page().await;
        self.cdp("Page.navigate", json!({ "url": target })).await?;
        self.wait_for_page(wait_ms).await;
        self.snapshot().await
    }

    pub async fn arm_teach(&self) {
        let _ = self.eval(TEACH_JS).await;
    }

    pub async fn drain_teach(&self) -> Vec<Value> {
        self.arm_teach().await;
        match self.eval(DRAIN_TEACH_JS).await.unwrap_or(Value::Null) {
            Value::Array(items) => items,
            _ => vec![],
        }
    }

    pub async fn snapshot(&self) -> Result<Value, String> {
        self.snapshot_find("").await
    }

    /// The page as the agent reads it. `find` (words such as a menu item or report name) lists matching elements
    /// first; when the page has more than MAX_ELEMENTS, the tree says how many were left out.
    pub async fn snapshot_find(&self, find: &str) -> Result<Value, String> {
        self.arm_teach().await;
        self.eval(WATCH_JS).await.ok();
        let find: String = find.chars().filter(|c| !c.is_control()).take(120).collect();
        let mut data = self.eval(&format!("({SNAPSHOT_JS})(0, {MAX_ELEMENTS}, {find:?})")).await?;
        if !data.is_object() {
            // Nothing came back (the page is between documents): say so rather than pass it off as an empty page.
            data = json!({ "url": "", "title": "", "text": "", "tree": "", "elements": [], "unread": true });
        }
        let mut total = data.get("total").and_then(Value::as_u64).unwrap_or(0) as usize;
        for ws_url in self.frame_targets().await {
            let start = data["elements"].as_array().map_or(0, Vec::len);
            if start >= MAX_ELEMENTS {
                break;
            }
            let limit = MAX_ELEMENTS - start;
            // A frame that can't be read (still loading, or just closed) shouldn't lose the rest of the page.
            if let Ok(frame) = self.eval_on(&ws_url, &format!("({SNAPSHOT_JS})({start}, {limit}, {find:?})")).await {
                total += frame.get("total").and_then(Value::as_u64).unwrap_or(0) as usize;
                merge_frame(&mut data, &frame);
            }
        }
        if let Some(obj) = data.as_object_mut() {
            obj.remove("total");
        }
        note_omitted(&mut data, total);
        let mut dialogs = self.eval(DRAIN_DIALOGS_JS).await.ok().and_then(|v| v.as_array().cloned()).unwrap_or_default();
        if DISMISSED_DIALOG.swap(false, std::sync::atomic::Ordering::Relaxed) {
            dialogs.push(json!({ "type": "unknown", "message": "A pop-up blocked the page and was closed.", "answer": "declined" }));
        }
        if !dialogs.is_empty() {
            data["dialogs"] = json!(dialogs);
        }
        if let Ok(shot) = self.screenshot_b64().await {
            data["screenshot_b64"] = json!(shot);
            data["shot_in_page_pixels"] = json!(true);
        }
        data["ok"] = json!(true);
        data["ran_on"] = json!("local_computer");
        Ok(data)
    }

    /// One image pixel per page pixel, on any display: a position in the image is a position to click, and the file is
    /// a quarter of the size on a Retina screen.
    pub async fn screenshot_b64(&self) -> Result<String, String> {
        let size = self.eval("[window.innerWidth, window.innerHeight, window.devicePixelRatio || 1]").await.unwrap_or(Value::Null);
        let part = |i: usize| size.get(i).and_then(Value::as_f64);
        let params = match (part(0), part(1), part(2)) {
            (Some(width), Some(height), Some(ratio)) if width > 0.0 && height > 0.0 && ratio > 0.0 => json!({
                "format": "png",
                "clip": { "x": 0, "y": 0, "width": width, "height": height, "scale": 1.0 / ratio }
            }),
            _ => json!({ "format": "png" }),
        };
        let result = self.cdp("Page.captureScreenshot", params).await?;
        result
            .get("data")
            .and_then(Value::as_str)
            .map(str::to_string)
            .ok_or_else(|| "no screenshot".into())
    }

    /// Wait for the page to settle after an action: DOM quiet and no spinner, capped (see SETTLE_JS).
    async fn settle(&self) {
        let _ = self
            .cdp_wait(
                "Runtime.evaluate",
                json!({ "expression": SETTLE_JS, "returnByValue": true, "awaitPromise": true }),
                Duration::from_secs(10),
            )
            .await;
    }

    async fn page_sig(&self) -> Option<String> {
        self.eval(PAGE_SIG_JS).await.ok().and_then(|v| v.as_str().map(str::to_string))
    }

    async fn picker(&self, target: &str, mode: &str, text: &str) -> Value {
        self.eval_on(target, &format!("({PICKER_JS})({mode:?}, {text:?})")).await.unwrap_or(Value::Null)
    }

    /// Fill a box that takes no typing, after it was clicked: pick the day in the calendar it opened (moving to the
    /// next month as needed), or type into the search box it opened and click the best suggestion.
    async fn fill_picker(&self, target: &str, text: &str, date_only: bool) -> Result<(), String> {
        tokio::time::sleep(Duration::from_millis(450)).await;
        for _ in 0..24 {
            let found = self.picker(target, "calendar", text).await;
            if let Some(cell) = found.get("cell") {
                let (x, y) = (cell["x"].as_i64().unwrap_or(0), cell["y"].as_i64().unwrap_or(0));
                self.mouse_at(target, x, y, 1).await?;
                return Ok(());
            }
            match found.get("next") {
                Some(next) => {
                    let (x, y) = (next["x"].as_i64().unwrap_or(0), next["y"].as_i64().unwrap_or(0));
                    self.mouse_at(target, x, y, 1).await?;
                    tokio::time::sleep(Duration::from_millis(350)).await;
                }
                None => break,
            }
        }
        if date_only {
            return Err(format!("couldn't find {text} in the calendar: take a snapshot and click the day, or use its month arrows"));
        }
        if self.picker(target, "focus", text).await.get("ok").and_then(Value::as_bool) != Some(true) {
            // No box to type in: a styled dropdown shows its choices as a list, so pick from that.
            let listed = self.picker(target, "suggest", text).await;
            if let Some(at) = listed.get("at") {
                let (x, y) = (at["x"].as_i64().unwrap_or(0), at["y"].as_i64().unwrap_or(0));
                self.mouse_at(target, x, y, 1).await?;
                tokio::time::sleep(Duration::from_millis(300)).await;
                return Ok(());
            }
            return Err("clicking this field opened nothing to type in; look at the page and pick the value from what opened".into());
        }
        self.cdp_on(target, "Input.insertText", json!({ "text": text }), Duration::from_secs(8)).await?;
        tokio::time::sleep(Duration::from_millis(900)).await;
        let hit = self.picker(target, "suggest", text).await;
        if let Some(at) = hit.get("at") {
            let (x, y) = (at["x"].as_i64().unwrap_or(0), at["y"].as_i64().unwrap_or(0));
            self.mouse_at(target, x, y, 1).await?;
        } else {
            for key in ["ArrowDown", "Enter"] {
                for phase in ["keyDown", "keyUp"] {
                    self.cdp_on(target, "Input.dispatchKeyEvent", key_event(phase, key), Duration::from_secs(8)).await?;
                }
            }
        }
        tokio::time::sleep(Duration::from_millis(300)).await;
        Ok(())
    }

    /// A real mouse at (x, y) on the main page: move there, and press / release `clicks` times (0 = hover only).
    async fn mouse_at(&self, target: &str, x: i64, y: i64, clicks: u64) -> Result<(), String> {
        let wait = Duration::from_secs(8);
        self.cdp_on(target, "Input.dispatchMouseEvent", json!({ "type": "mouseMoved", "x": x, "y": y }), wait).await?;
        for n in 1..=clicks {
            for phase in ["mousePressed", "mouseReleased"] {
                self.cdp_on(
                    target,
                    "Input.dispatchMouseEvent",
                    json!({ "type": phase, "x": x, "y": y, "button": "left", "clickCount": n }),
                    wait,
                )
                .await?;
            }
        }
        Ok(())
    }

    pub async fn act(&self, actions: &[Value]) -> Result<Value, String> {
        let mut warnings: Vec<String> = vec![];
        self.eval(WATCH_JS).await.ok();
        // A real mouse needs the page in front; several chats can be working in their own tabs.
        let front = self.tab.lock().ok().and_then(|current| current.clone());
        if let Some(id) = front {
            let _ = self.http.get(format!("http://127.0.0.1:{}/json/activate/{id}", self.port)).send().await;
        }
        for (index, step) in actions.iter().enumerate() {
            let kind = step.get("kind").and_then(Value::as_str).unwrap_or("click");
            let check_effect = matches!(kind, "click" | "dblclick" | "hover" | "check" | "uncheck");
            let sig_before = if check_effect { self.page_sig().await } else { None };
            if kind == "wait" {
                let ms = step.get("ms").and_then(Value::as_u64).unwrap_or(800);
                tokio::time::sleep(Duration::from_millis(ms)).await;
                continue;
            }
            if kind == "reload" {
                self.cdp("Page.reload", json!({})).await.ok();
                tokio::time::sleep(Duration::from_millis(800)).await;
                continue;
            }
            if kind == "back" {
                self.eval("history.back()").await.ok();
                tokio::time::sleep(Duration::from_millis(600)).await;
                continue;
            }
            if kind == "accept_dialog" {
                let answer = step.get("text").or_else(|| step.get("value")).and_then(Value::as_str).unwrap_or("");
                self.eval(&format!("window.__lumioWatch.accept({answer:?})")).await?;
                continue;
            }
            if kind == "drag" {
                let from = step.get("ref").and_then(Value::as_str).unwrap_or("");
                let to = step.get("to").or_else(|| step.get("to_ref")).and_then(Value::as_str).unwrap_or("");
                let page = self.page_ws().await?;
                let at = |x: &str, y: &str| Some((step.get(x)?.as_i64()?, step.get(y)?.as_i64()?));
                let start = if from.is_empty() {
                    at("x", "y").ok_or_else(|| "drag needs ref or x and y".to_string())?
                } else {
                    self.point_of(&page, from).await?
                };
                let end = if to.is_empty() {
                    at("x2", "y2").ok_or_else(|| "drag needs to or x2 and y2".to_string())?
                } else {
                    self.point_of(&page, to).await?
                };
                self.drag_between(&page, start, end).await?;
                continue;
            }
            if kind == "upload" {
                let raw = step.get("path").or_else(|| step.get("file")).or_else(|| step.get("text")).and_then(Value::as_str).unwrap_or("");
                let path = match crate::grants::resolve_existing(raw) {
                    Ok(path) => path,
                    Err(err) => return Err(err.to_json().get("error").and_then(Value::as_str).unwrap_or("that file can't be used").to_string()),
                };
                let from = step.get("ref").and_then(Value::as_str).unwrap_or("");
                let page = self.page_ws().await?;
                self.upload_on(&page, from, &path.to_string_lossy()).await?;
                continue;
            }
            if kind == "forward" {
                self.eval("history.forward()").await.ok();
                tokio::time::sleep(Duration::from_millis(600)).await;
                continue;
            }
            if kind == "wait_for" || kind == "waitfor" {
                let want = step.get("text").or_else(|| step.get("value")).and_then(Value::as_str).unwrap_or("");
                let probe = format!("((document.body && document.body.innerText) || '').toLowerCase().includes({:?})", want.to_lowercase());
                let mut found = want.is_empty();
                for _ in 0..40 {
                    if found {
                        break;
                    }
                    found = self.eval(&probe).await.ok().and_then(|v| v.as_bool()) == Some(true);
                    if !found {
                        tokio::time::sleep(Duration::from_millis(250)).await;
                    }
                }
                if !found {
                    return Err(format!("step {}: \"{want}\" did not appear on the page within 10 seconds", index + 1));
                }
                continue;
            }
            if kind == "scroll" {
                let dy = step.get("dy").and_then(Value::as_i64).or_else(|| step.get("amount").and_then(Value::as_i64)).unwrap_or(800);
                self.eval(&format!("window.scrollBy(0, {dy})")).await.ok();
                continue;
            }
            if let (Some(x), Some(y)) = (step.get("x").and_then(Value::as_i64), step.get("y").and_then(Value::as_i64)) {
                let page = self.page_ws().await?;
                match kind {
                    "rightclick" | "contextclick" | "context_click" => self.mouse_right(&page, x, y).await?,
                    "dblclick" => self.mouse_at(&page, x, y, 2).await?,
                    _ => self.mouse_at(&page, x, y, 1).await?,
                }
                self.settle().await;
                continue;
            }
            if kind == "type" && step.get("ref").is_none() {
                // Working from the screen: type into whatever was just clicked.
                let text = step.get("text").and_then(Value::as_str).unwrap_or("");
                self.type_text(text).await?;
                continue;
            }
            let ref_id = step.get("ref").and_then(Value::as_str).unwrap_or("");
            let text = step.get("text").or_else(|| step.get("value")).and_then(Value::as_str).unwrap_or("");
            // A real mouse only on the main page: an other-site frame's coordinates are not the page's.
            let js = format!("({ACT_JS})({ref_id:?}, {kind:?}, {text:?}, true)");
            let frame_js = format!("({ACT_JS})({ref_id:?}, {kind:?}, {text:?}, false)");
            // The ref is on the page or in one of its other-site frames. Keyboard input goes to whichever has it.
            let mut target = self.page_ws().await?;
            let mut result = self.eval_on(&target, &js).await?;
            if result.get("not_found").and_then(Value::as_bool) == Some(true) {
                for ws_url in self.frame_targets().await {
                    if let Ok(found) = self.eval_on(&ws_url, &frame_js).await {
                        if found.get("not_found").and_then(Value::as_bool) != Some(true) {
                            result = found;
                            target = ws_url;
                            break;
                        }
                    }
                }
            }
            if kind == "press" && result.get("not_found").and_then(Value::as_bool) == Some(true) {
                // A stale ref on a key press falls back to whatever has focus.
                result = self.eval_on(&target, &format!("({ACT_JS})(\"\", {kind:?}, {text:?})")).await?;
            }
            if result.get("ok").and_then(Value::as_bool) == Some(false) {
                let reason = result.get("error").and_then(Value::as_str).unwrap_or("browser action failed");
                // In a batch, the earlier steps already happened: say so, or they get done twice.
                return Err(if index > 0 { format!("step {} failed, the {index} before it ran: {reason}", index + 1) } else { reason.to_string() });
            }
            if result.get("already").and_then(Value::as_bool) == Some(true) {
                let chosen = result.get("selected").and_then(Value::as_str).unwrap_or("");
                warnings.push(format!("{chosen} was already chosen in this dropdown, so nothing needed to change. Carry on with the next step."));
            }
            if let Some(at) = result.get("mouse") {
                let (x, y) = (at.get("x").and_then(Value::as_i64), at.get("y").and_then(Value::as_i64));
                if let (Some(x), Some(y)) = (x, y) {
                    if result.get("button").and_then(Value::as_str) == Some("right") {
                        self.mouse_right(&target, x, y).await?;
                    } else {
                        let clicks = result.get("clicks").and_then(Value::as_u64).unwrap_or(1);
                        self.mouse_at(&target, x, y, clicks).await?;
                    }
                }
            }
            if result.get("picker").and_then(Value::as_bool) == Some(true) {
                let date_only = result.get("dateOnly").and_then(Value::as_bool) == Some(true);
                self.fill_picker(&target, text, date_only).await?;
            }
            if result.get("insert").and_then(Value::as_bool) == Some(true) && !text.is_empty() {
                self.cdp_on(&target, "Input.insertText", json!({ "text": text }), Duration::from_secs(8)).await?;
            }
            if result.get("submit").and_then(Value::as_bool) == Some(true) {
                let key = result.get("key").and_then(Value::as_str).unwrap_or("Enter");
                for phase in ["keyDown", "keyUp"] {
                    self.cdp_on(&target, "Input.dispatchKeyEvent", key_event(phase, key), Duration::from_secs(8)).await?;
                }
                tokio::time::sleep(Duration::from_millis(450)).await;
                if key.eq_ignore_ascii_case("enter") || key.eq_ignore_ascii_case("return") {
                    self.eval_on(&target, SUBMIT_IF_DRAFT_JS).await.ok();
                }
            }
            if check_effect {
                self.settle().await;
                if sig_before.is_some() && self.page_sig().await == sig_before {
                    // Some pages ignore the mouse here: try the page's own click once, unless it's a final control.
                    let mut retried = false;
                    if kind == "click" && result.get("mouse").is_some() {
                        let again = format!("({ACT_JS})({ref_id:?}, \"dom_click\", \"\", false)");
                        if let Ok(done) = self.eval_on(&target, &again).await {
                            if done.get("ok").and_then(Value::as_bool) == Some(true) && done.get("skipped").is_none() {
                                self.settle().await;
                                retried = self.page_sig().await != sig_before;
                            }
                        }
                    }
                    if retried {
                        warnings.push(format!("step {} ({kind} {ref_id}): the mouse click had no effect, so it was clicked through the page instead, which worked.", index + 1));
                    } else {
                        warnings.push(no_effect_note(index, kind, ref_id));
                    }
                }
            }
        }
        // A click step has already waited for the page; waiting again only adds time.
        let settled = actions.last().is_some_and(|s| {
            s.get("x").is_none() && matches!(s.get("kind").and_then(Value::as_str).unwrap_or("click"), "click" | "dblclick" | "hover" | "check" | "uncheck")
        });
        if !settled {
            self.settle().await;
        }
        let mut data = self.snapshot().await?;
        // A full screenshot with every step is slow to upload, and the live panel doesn't need one more often than this.
        if !shot_due() {
            if let Some(obj) = data.as_object_mut() {
                obj.remove("screenshot_b64");
            }
        }
        if !warnings.is_empty() {
            data["warnings"] = json!(warnings);
        }
        Ok(data)
    }

    pub async fn type_text(&self, text: &str) -> Result<(), String> {
        self.cdp("Input.insertText", json!({ "text": text })).await?;
        Ok(())
    }

    pub async fn key(&self, key: &str) -> Result<(), String> {
        self.cdp("Input.dispatchKeyEvent", key_event("keyDown", key)).await?;
        self.cdp("Input.dispatchKeyEvent", key_event("keyUp", key)).await?;
        Ok(())
    }

    pub async fn computer_act(&self, actions: &[Value]) -> Result<Value, String> {
        for step in actions {
            let kind = step.get("kind").and_then(Value::as_str).unwrap_or("click");
            match kind {
                "wait" => {
                    let ms = step.get("ms").and_then(Value::as_u64).unwrap_or(500);
                    tokio::time::sleep(Duration::from_millis(ms)).await;
                }
                "type" | "fill" => {
                    let text = step.get("text").and_then(Value::as_str).unwrap_or("");
                    self.type_text(text).await.ok();
                }
                "key" | "press" => {
                    let key = step.get("key").and_then(Value::as_str).or_else(|| step.get("text").and_then(Value::as_str)).unwrap_or("Enter");
                    self.key(key).await.ok();
                }
                "scroll" => {
                    let dy = step.get("dy").and_then(Value::as_i64).or_else(|| step.get("amount").and_then(Value::as_i64)).unwrap_or(800);
                    self.eval(&format!("window.scrollBy(0, {dy})")).await.ok();
                }
                _ => {
                    if let (Some(x), Some(y)) = (step.get("x").and_then(Value::as_i64), step.get("y").and_then(Value::as_i64)) {
                        self.cdp(
                            "Input.dispatchMouseEvent",
                            json!({ "type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1 }),
                        )
                        .await
                        .ok();
                        self.cdp(
                            "Input.dispatchMouseEvent",
                            json!({ "type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1 }),
                        )
                        .await
                        .ok();
                    }
                }
            }
        }
        self.snapshot().await
    }

    pub async fn search(&self, query: &str) -> Result<Value, String> {
        let q = urlencoding_lite(query);
        self.navigate(&format!("https://www.google.com/search?q={q}"), 1500).await
    }

    pub async fn collect(&self, args: &Value) -> Result<Value, String> {
        collect_listing(self, args).await
    }

    pub async fn page_info(&self) -> Result<(String, String, String), String> {
        let shot = self.screenshot_b64().await.unwrap_or_default();
        let url = self.eval("location.href").await.ok().and_then(|v| v.as_str().map(str::to_string)).unwrap_or_default();
        let title = self.eval("document.title").await.ok().and_then(|v| v.as_str().map(str::to_string)).unwrap_or_default();
        Ok((shot, url, title))
    }
}

const COLLECT_PASSES: usize = 80;
const COLLECT_CAP: usize = 400;
const REVIEW_BATCH: usize = 80;

struct SavedCollect {
    key: String,
    scanned: usize,
    unpriced: usize,
    stopped: String,
    matched: Vec<Value>,
}

fn collect_cache() -> &'static std::sync::Mutex<Option<SavedCollect>> {
    static CACHE: std::sync::OnceLock<std::sync::Mutex<Option<SavedCollect>>> = std::sync::OnceLock::new();
    CACHE.get_or_init(|| std::sync::Mutex::new(None))
}

fn collect_part(marker: &str) -> Result<&'static str, String> {
    let key = format!("/* {marker} */");
    let Some(at) = COLLECT_JS.find(&key) else {
        return Err(format!("collector script missing {marker}"));
    };
    let rest = &COLLECT_JS[at + key.len()..];
    let Some(end) = rest.find("/* lumio-collect-end */") else {
        return Err(format!("collector script missing end after {marker}"));
    };
    Ok(rest[..end].trim())
}

fn json_f64(value: &Value) -> Option<f64> {
    value
        .as_f64()
        .or_else(|| value.as_i64().map(|n| n as f64))
        .or_else(|| value.as_u64().map(|n| n as f64))
        .or_else(|| value.as_str().and_then(|s| s.parse().ok()))
}

fn opt_price(args: &Value, key: &str) -> Option<f64> {
    args.get(key).and_then(json_f64)
}

fn split_amounts(amounts: &Value) -> (Option<f64>, Option<f64>, Vec<f64>) {
    let mut struck = Vec::new();
    let mut plain = Vec::new();
    for item in amounts.as_array().into_iter().flatten() {
        let Some(value) = item.get("value").and_then(json_f64) else { continue };
        if value <= 0.0 {
            continue;
        }
        if item.get("struck").and_then(Value::as_bool).unwrap_or(false) {
            struck.push(value);
        } else {
            plain.push(value);
        }
    }
    let mut other = Vec::new();
    if !plain.is_empty() {
        let selling = plain[0];
        other.extend(plain.iter().skip(1).copied());
        if !struck.is_empty() {
            let mrp = struck.iter().copied().fold(f64::MIN, f64::max);
            other.extend(struck.iter().copied().filter(|v| (*v - mrp).abs() > 0.001));
            return (Some(selling), Some(mrp), other);
        }
        return (Some(selling), None, other);
    }
    if !struck.is_empty() {
        let selling = struck[0];
        other.extend(struck.iter().skip(1).copied());
        return (Some(selling), None, other);
    }
    (None, None, other)
}

fn canon_url(raw: &str) -> String {
    let text = raw.trim();
    if !(text.starts_with("https://") || text.starts_with("http://")) {
        return String::new();
    }
    text.split('#').next().unwrap_or("").to_string()
}

fn title_ok(title: &str, words: &str) -> bool {
    let low = title.to_lowercase();
    words.split_whitespace().all(|word| low.contains(&word.to_lowercase()))
}

fn in_price(price: f64, low: Option<f64>, high: Option<f64>) -> bool {
    if let Some(min) = low {
        if price < min {
            return false;
        }
    }
    if let Some(max) = high {
        if price > max {
            return false;
        }
    }
    true
}

enum CardFate {
    Skip,
    Unpriced,
    Drop,
    Keep(Value),
}

fn card_fate(card: &Value, low: Option<f64>, high: Option<f64>, words: &str) -> CardFate {
    let url = canon_url(card.get("url").and_then(Value::as_str).unwrap_or(""));
    if url.is_empty() {
        return CardFate::Skip;
    }
    let title = card.get("title").and_then(Value::as_str).unwrap_or("").split_whitespace().collect::<Vec<_>>().join(" ");
    let (selling, mrp, other) = split_amounts(card.get("amounts").unwrap_or(&Value::Null));
    let Some(selling) = selling else { return CardFate::Unpriced };
    if title.len() < 2 {
        return CardFate::Unpriced;
    }
    if !title_ok(&title, words) || !in_price(selling, low, high) {
        return CardFate::Drop;
    }
    CardFate::Keep(json!({
        "title": title.chars().take(180).collect::<String>(),
        "url": url,
        "selling_price": selling,
        "mrp": mrp,
        "other_prices": other,
        "rating": card.get("rating").cloned().unwrap_or(Value::Null),
        "rating_count": card.get("rating_count").cloned().unwrap_or(Value::Null),
        "review_count": card.get("review_count").cloned().unwrap_or(Value::Null),
    }))
}

fn apply_review(row: &mut Value, info: &Value) {
    let (selling, mrp, other) = split_amounts(info.get("amounts").unwrap_or(&Value::Null));
    if let Some(selling) = selling {
        let current = row.get("selling_price").and_then(json_f64);
        if current != Some(selling) {
            let mut extra = row.get("other_prices").and_then(Value::as_array).cloned().unwrap_or_default();
            if let Some(prev) = current {
                extra.push(json!(prev));
            }
            for price in other {
                extra.push(json!(price));
            }
            row["other_prices"] = Value::Array(extra);
            row["selling_price"] = json!(selling);
        }
    }
    if let Some(mrp) = mrp {
        row["mrp"] = json!(mrp);
    }
    if let Some(rating) = info.get("rating").and_then(json_f64) {
        row["rating"] = json!(rating);
    }
    if info.get("rating_count").map(|v| !v.is_null()).unwrap_or(false) {
        row["rating_count"] = info["rating_count"].clone();
    }
    if info.get("review_count").map(|v| !v.is_null()).unwrap_or(false) {
        row["review_count"] = info["review_count"].clone();
    }
    let mut reviews = Vec::new();
    for text in info.get("reviews").and_then(Value::as_array).into_iter().flatten() {
        let snippet = text.as_str().unwrap_or("").split_whitespace().collect::<Vec<_>>().join(" ");
        if snippet.is_empty() {
            continue;
        }
        reviews.push(Value::String(snippet.chars().take(400).collect()));
        if reviews.len() >= 8 {
            break;
        }
    }
    row["reviews"] = Value::Array(reviews.clone());
    row["reviews_shown"] = json!(reviews.len());
    row["reviews_complete"] = json!(false);
}

async fn read_review(chrome: &Chrome, row: &mut Value, review_js: &str, read_js: &str) {
    let url = row.get("url").and_then(Value::as_str).unwrap_or("").to_string();
    if chrome.cdp_wait("Page.navigate", json!({ "url": url }), Duration::from_secs(20)).await.is_err() {
        row["reviews"] = json!([]);
        row["reviews_shown"] = json!(0);
        row["reviews_complete"] = json!(false);
        row["review_error"] = json!("could not open the product page");
        return;
    }
    chrome.wait_for_page(1500).await;
    let info = chrome.eval_wait(review_js, Duration::from_secs(20)).await.unwrap_or(Value::Null);
    let mut info = if info.is_object() { info } else { json!({}) };
    if info.get("clicked").and_then(Value::as_bool).unwrap_or(false) {
        tokio::time::sleep(Duration::from_millis(700)).await;
        if let Ok(next) = chrome.eval_wait(read_js, Duration::from_secs(20)).await {
            if next.is_object() {
                info = next;
            }
        }
    }
    apply_review(row, &info);
}

async fn scroll_collect(chrome: &Chrome, extract_js: &str, scroll_js: &str) -> Result<(Vec<Value>, String), String> {
    let mut seen: Vec<String> = Vec::new();
    let mut cards: Vec<Value> = Vec::new();
    let mut stall = 0usize;
    let mut stopped = "end".to_string();
    let mut finished = false;
    for _ in 0..COLLECT_PASSES {
        let found = chrome.eval_wait(extract_js, Duration::from_secs(20)).await.unwrap_or(Value::Null);
        let mut added = 0usize;
        for card in found.as_array().into_iter().flatten() {
            let url = canon_url(card.get("url").and_then(Value::as_str).unwrap_or(""));
            if url.is_empty() || seen.iter().any(|have| have == &url) {
                continue;
            }
            seen.push(url);
            cards.push(card.clone());
            added += 1;
        }
        if cards.len() >= COLLECT_CAP {
            stopped = "cap".to_string();
            finished = true;
            break;
        }
        let state = chrome.eval_wait(scroll_js, Duration::from_secs(15)).await.unwrap_or(json!({}));
        tokio::time::sleep(Duration::from_millis(450)).await;
        let moved = state.get("moved").and_then(Value::as_bool).unwrap_or(false);
        if added == 0 {
            stall += 1;
        } else {
            stall = 0;
        }
        if stall >= 2 && !moved {
            stopped = "end".to_string();
            finished = true;
            break;
        }
        if stall >= 2 && moved {
            stopped = "stalled".to_string();
            finished = true;
            break;
        }
    }
    if !finished {
        stopped = if cards.len() >= COLLECT_CAP { "cap" } else { "stalled" }.to_string();
    }
    Ok((cards, stopped))
}

async fn collect_listing(chrome: &Chrome, args: &Value) -> Result<Value, String> {
    let extract_js = collect_part("lumio-collect-cards")?;
    let scroll_js = collect_part("lumio-collect-scroll")?;
    let review_fn = collect_part("lumio-collect-review-fn")?;
    let review_js = format!("({review_fn})(true)");
    let read_js = format!("({review_fn})(false)");
    let low = opt_price(args, "min_price");
    let high = opt_price(args, "max_price");
    let words = args.get("words").and_then(Value::as_str).unwrap_or("");
    let with_reviews = args.get("with_reviews").and_then(Value::as_bool).unwrap_or(false);
    let offset = args.get("offset").and_then(|v| v.as_u64().or_else(|| v.as_i64().map(|n| n.max(0) as u64))).unwrap_or(0) as usize;
    let key = format!("{:?}|{:?}|{}|{}", low, high, words.to_lowercase(), with_reviews);
    let cached = if offset > 0 {
        collect_cache().lock().ok().and_then(|guard| {
            guard.as_ref().filter(|saved| saved.key == key).map(|saved| {
                (saved.matched.clone(), saved.scanned, saved.unpriced, saved.stopped.clone())
            })
        })
    } else {
        None
    };
    let (matched, scanned, unpriced, stopped) = if let Some(saved) = cached {
        saved
    } else {
        let (cards, stopped) = scroll_collect(chrome, extract_js, scroll_js).await?;
        let mut matched = Vec::new();
        let mut seen_urls = Vec::new();
        let mut unpriced = 0usize;
        for card in &cards {
            let url = canon_url(card.get("url").and_then(Value::as_str).unwrap_or(""));
            if url.is_empty() || seen_urls.iter().any(|have| have == &url) {
                continue;
            }
            seen_urls.push(url);
            match card_fate(card, low, high, words) {
                CardFate::Keep(row) => matched.push(row),
                CardFate::Unpriced => unpriced += 1,
                CardFate::Skip | CardFate::Drop => {}
            }
        }
        let scanned = seen_urls.len();
        if let Ok(mut guard) = collect_cache().lock() {
            *guard = Some(SavedCollect { key, scanned, unpriced, stopped: stopped.clone(), matched: matched.clone() });
        }
        (matched, scanned, unpriced, stopped)
    };
    let mut rows = matched.clone();
    let mut next_offset = Value::Null;
    if with_reviews {
        let end = (offset + REVIEW_BATCH).min(matched.len());
        let start = offset.min(matched.len());
        let mut batch = Vec::new();
        for row in matched.iter().skip(start).take(end.saturating_sub(start)) {
            let mut copy = row.clone();
            read_review(chrome, &mut copy, &review_js, &read_js).await;
            batch.push(copy);
        }
        next_offset = if end < matched.len() { json!(end) } else { Value::Null };
        rows = batch;
    }
    Ok(json!({
        "ok": true,
        "scanned": scanned,
        "matched": matched.len(),
        "unpriced": unpriced,
        "stopped": stopped,
        "rows": rows,
        "next_offset": next_offset,
        "ran_on": "local_computer",
        "note": "Selling price is the non-struck amount, or the only amount on the card. It can still vary by size. Reviews are the ones shown on the page, not a full export.",
    }))
}

fn key_event(phase: &str, key: &str) -> Value {
    let name = if key.eq_ignore_ascii_case("return") { "Enter" } else { key };
    let (code, vk) = match name {
        "Enter" => ("Enter", 13),
        "Backspace" => ("Backspace", 8),
        "Tab" => ("Tab", 9),
        "Escape" => ("Escape", 27),
        "ArrowDown" => ("ArrowDown", 40),
        "ArrowUp" => ("ArrowUp", 38),
        "ArrowLeft" => ("ArrowLeft", 37),
        "ArrowRight" => ("ArrowRight", 39),
        "Delete" => ("Delete", 46),
        " " | "Space" => ("Space", 32),
        _ => ("", 0),
    };
    let mut event = json!({
        "type": phase,
        "key": name,
        "windowsVirtualKeyCode": vk,
        "nativeVirtualKeyCode": vk,
    });
    if !code.is_empty() {
        event["code"] = json!(code);
    }
    event
}

fn urlencoding_lite(raw: &str) -> String {
    raw.bytes()
        .map(|b| match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => (b as char).to_string(),
            b' ' => "+".into(),
            _ => format!("%{b:02X}"),
        })
        .collect()
}

#[cfg(test)]
mod frame_tests {
    use super::*;

    fn snapshot() -> Value {
        json!({ "url": "https://a.test/", "text": "page", "tree": "- e0 button Go", "composer": "", "elements": [{ "ref": "e0", "x": 5, "y": 5 }] })
    }

    #[test]
    fn a_cut_element_list_says_how_many_are_left_out() {
        let mut page = snapshot();
        note_omitted(&mut page, 1);
        assert!(page.get("elements_omitted").is_none());
        note_omitted(&mut page, 41);
        assert_eq!(page["elements_omitted"], 40);
        assert!(page["tree"].as_str().unwrap().ends_with("to list the matching ones first."));
        assert!(page["tree"].as_str().unwrap().contains("… 40 more elements"));
    }

    #[test]
    fn a_click_that_changed_nothing_is_named() {
        let note = no_effect_note(2, "click", "e9");
        assert!(note.starts_with("step 3 (click e9) may not have done anything"));
        assert!(note.contains("hover first"));
    }

    #[test]
    fn act_script_moves_a_real_mouse_and_reads_dates() {
        assert!(ACT_JS.contains("allowMouse") && ACT_JS.contains("mouse: at"));
        assert!(ACT_JS.contains("nativeDate") && ACT_JS.contains("datetime-local"));
        assert!(SNAPSHOT_JS.contains("(start, limit, find)") && SNAPSHOT_JS.contains("total: ordered.length"));
        assert!(SETTLE_JS.contains("aria-busy") && PAGE_SIG_JS.contains("aria-expanded"));
        assert!(ACT_JS.contains("picker: true") && PICKER_JS.contains("mode === 'calendar'") && PICKER_JS.contains("mode === 'suggest'"));
    }

    #[test]
    fn only_this_pages_frames_are_used() {
        let targets = vec![
            json!({ "id": "p1", "type": "page", "webSocketDebuggerUrl": "ws://p1" }),
            json!({ "id": "p2", "type": "page", "webSocketDebuggerUrl": "ws://p2" }),
            json!({ "id": "f1", "type": "iframe", "parentId": "p1", "webSocketDebuggerUrl": "ws://f1" }),
            json!({ "id": "f2", "type": "iframe", "parentId": "p2", "webSocketDebuggerUrl": "ws://f2" }),
            json!({ "id": "w1", "type": "worker", "parentId": "p1", "webSocketDebuggerUrl": "ws://w1" }),
        ];
        assert_eq!(frames_of_page(&targets, "ws://p1"), vec!["ws://f1"]);
        assert!(frames_of_page(&targets, "ws://gone").is_empty());
    }

    #[test]
    fn a_frame_is_merged_without_its_coordinates() {
        let mut page = snapshot();
        let frame = json!({ "url": "https://pay.test/", "text": "Card details", "tree": "- e1 textbox Card", "composer": "note", "elements": [{ "ref": "e1", "x": 9, "y": 9 }] });
        merge_frame(&mut page, &frame);
        let rows = page["elements"].as_array().unwrap();
        assert_eq!(rows.len(), 2);
        assert!(rows[1].get("x").is_none() && rows[1].get("y").is_none());
        assert_eq!(rows[1]["frame"], "https://pay.test/");
        assert!(page["tree"].as_str().unwrap().contains("[frame https://pay.test/]\n- e1 textbox Card"));
        assert!(page["text"].as_str().unwrap().contains("Card details"));
        assert_eq!(page["composer"], "note");
    }

    #[test]
    fn an_empty_frame_is_left_out() {
        let mut page = snapshot();
        merge_frame(&mut page, &json!({ "url": "https://ads.test/", "text": " ", "tree": "", "elements": [] }));
        assert_eq!(page["elements"].as_array().unwrap().len(), 1);
        assert!(!page["text"].as_str().unwrap().contains("ads.test"));
    }

    #[test]
    fn frame_text_is_capped() {
        let mut page = snapshot();
        let long = "x".repeat(MAX_FRAME_TEXT * 2);
        merge_frame(&mut page, &json!({ "url": "https://b.test/", "text": long, "tree": "", "elements": [] }));
        assert!(page["text"].as_str().unwrap().len() < MAX_FRAME_TEXT + 100);
    }

    const MAIN_PAGE: &str = r#"<!doctype html><title>Main</title><body>
      <button id="plain">Plain button</button>
      <input id="pw" type="password" autocomplete="current-password">
      <input id="card" autocomplete="cc-number" value="4242424242424242">
      <div id="host"></div>
      <iframe id="same" src="/inner" width="400" height="200"></iframe>
      <iframe id="other" src="OTHER/frame" width="400" height="200"></iframe>
      <script>
        document.getElementById('pw').value = 'hunter22-secret';
        const root = document.getElementById('host').attachShadow({ mode: 'open' });
        root.innerHTML = '<p>Shadow words</p><input id="in-shadow" placeholder="Shadow field">';
      </script></body>"#;
    const INNER_PAGE: &str = r#"<!doctype html><body><p>Same origin words</p><input id="in-same" placeholder="Same field"></body>"#;
    const FRAME_PAGE: &str = r#"<!doctype html><body><p>Other site words</p>
      <input id="in-other" placeholder="Other field" type="text">
      <input id="pw-other" type="password" placeholder="Other password"></body>"#;
    const SIGN_IN: &str = r#"<!doctype html><title>Sign in</title><body><input placeholder="Email"></body>"#;
    const EMPTY: &str = r#"<!doctype html><title>Nothing</title><body></body>"#;

    async fn serve(listener: tokio::net::TcpListener, other: String) {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        loop {
            let Ok((mut sock, _)) = listener.accept().await else { return };
            let other = other.clone();
            tokio::spawn(async move {
                let mut buf = [0u8; 2048];
                let n = sock.read(&mut buf).await.unwrap_or(0);
                let head = String::from_utf8_lossy(&buf[..n]);
                let path = head.split_whitespace().nth(1).unwrap_or("/");
                let body = match path {
                    "/inner" => INNER_PAGE.to_string(),
                    "/frame" => FRAME_PAGE.to_string(),
                    "/signin" => SIGN_IN.to_string(),
                    "/empty" => EMPTY.to_string(),
                    _ => MAIN_PAGE.replace("OTHER", &other),
                };
                let reply = format!("HTTP/1.1 200 OK\r\ncontent-type: text/html\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{body}", body.len());
                let _ = sock.write_all(reply.as_bytes()).await;
            });
        }
    }

    fn ref_of(snapshot: &Value, name: &str) -> String {
        snapshot["elements"]
            .as_array()
            .unwrap()
            .iter()
            .find(|e| e["name"].as_str().is_some_and(|n| n.contains(name)))
            .unwrap_or_else(|| panic!("no element named {name}: {}", snapshot["tree"]))["ref"]
            .as_str()
            .unwrap()
            .to_string()
    }

    /// Drives a real headless Chrome. Run with: cargo test frame_tests -- --ignored
    #[test]
    #[ignore = "needs Google Chrome"]
    fn real_chrome_reads_and_fills_frames_and_shadow_dom() {
        let rt = tokio::runtime::Builder::new_multi_thread().enable_all().build().unwrap();
        rt.block_on(async {
            let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
            let web = listener.local_addr().unwrap().port();
            // 127.0.0.1 and localhost are different sites, so this frame gets its own Chrome target.
            tokio::spawn(serve(listener, format!("http://localhost:{web}")));

            let bin = chrome_candidates().into_iter().next().expect("Chrome is installed");
            let port = 9444u16;
            let profile = std::env::temp_dir().join(format!("lumio-frame-test-{web}"));
            let mut child = Command::new(&bin)
                .arg("--headless=new")
                .arg(format!("--remote-debugging-port={port}"))
                .arg(format!("--user-data-dir={}", profile.display()))
                .arg("--no-first-run")
                .arg("--site-per-process")
                .arg("--window-size=1280,800")
                .arg(format!("http://127.0.0.1:{web}/empty"))
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .kill_on_drop(true)
                .spawn()
                .unwrap();
            let http = http_client(Duration::from_secs(3)).unwrap();
            wait_debug(&http, port).await.unwrap();
            let chrome = Chrome { _child: None, port, http, tab: std::sync::Mutex::new(None) };

            let t0 = std::time::Instant::now();
            let page = chrome.navigate(&format!("http://127.0.0.1:{web}/signin"), 6000).await.unwrap();
            assert!(t0.elapsed() < Duration::from_millis(3000), "short sign-in page took {:?}", t0.elapsed());
            assert!(page["tree"].as_str().unwrap().contains("Email"));

            let page = chrome.navigate(&format!("http://127.0.0.1:{web}/"), 6000).await.unwrap();
            tokio::time::sleep(Duration::from_millis(800)).await;
            let page = if page["tree"].as_str().unwrap().contains("Other field") { page } else { chrome.snapshot().await.unwrap() };
            let mut without_shot = page.clone();
            without_shot.as_object_mut().unwrap().remove("screenshot_b64");
            let dump = without_shot.to_string();
            assert!(dump.contains("Shadow words") && dump.contains("Same origin words") && dump.contains("Other site words"), "{dump}");
            assert!(!dump.contains("hunter22-secret"), "password leaked: {dump}");
            assert!(!dump.contains("4242424242424242"), "card number leaked: {dump}");

            let refs: Vec<&str> = page["elements"].as_array().unwrap().iter().filter_map(|e| e["ref"].as_str()).collect();
            let unique: std::collections::HashSet<_> = refs.iter().collect();
            assert_eq!(refs.len(), unique.len(), "refs must not repeat: {refs:?}");

            for (name, frame_id, script_target) in [
                ("Shadow field", "in-shadow", "shadow"),
                ("Same field", "in-same", "same"),
                ("Other field", "in-other", "other"),
            ] {
                let target = ref_of(&page, name);
                chrome.act(&[json!({ "kind": "fill", "ref": target, "text": format!("typed {name}") })]).await.unwrap_or_else(|e| panic!("{name}: {e}"));
                let read = match script_target {
                    "shadow" => chrome.eval(&format!("document.getElementById('host').shadowRoot.getElementById('{frame_id}').value")).await.unwrap(),
                    "same" => chrome.eval(&format!("document.getElementById('same').contentDocument.getElementById('{frame_id}').value")).await.unwrap(),
                    _ => {
                        let frames = chrome.frame_targets().await;
                        assert!(!frames.is_empty(), "the other-site frame should be its own target");
                        chrome.eval_on(&frames[0], &format!("document.getElementById('{frame_id}').value")).await.unwrap()
                    }
                };
                assert_eq!(read, json!(format!("typed {name}")), "{name}");
            }

            // A secret typed into the other-site frame stays out of what comes back.
            let pw = ref_of(&page, "Other password");
            let after = chrome.act(&[json!({ "kind": "fill", "ref": pw, "text": "frame-secret-77" })]).await.unwrap();
            let mut after = after;
            after.as_object_mut().unwrap().remove("screenshot_b64");
            assert!(!after.to_string().contains("frame-secret-77"), "frame password leaked");

            // Clicking in the page still works, and a ref that doesn't exist is an error.
            assert!(chrome.act(&[json!({ "kind": "click", "ref": "e9999" })]).await.is_err());
            chrome.act(&[json!({ "kind": "press", "ref": "e9999", "text": "Tab" })]).await.unwrap();
            let plain = ref_of(&page, "Plain button");
            chrome.act(&[json!({ "kind": "click", "ref": plain })]).await.unwrap();

            child.kill().await.ok();
        });
    }
}
