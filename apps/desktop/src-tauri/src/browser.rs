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

pub struct Chrome {
    _child: Option<Child>,
    port: u16,
    http: reqwest::Client,
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
    Ok(Chrome { _child: None, port, http })
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
        // A window may already exist with no page showing. Open this address so the user sees it.
        let _ = http.get(format!("http://127.0.0.1:{port}/json/new?{start_url}")).send().await;
        return Ok(Chrome { _child: None, port, http });
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
    Ok(Chrome { _child: Some(child), port, http })
}

impl Chrome {
    async fn pages(&self) -> Result<Vec<Value>, String> {
        let url = format!("http://127.0.0.1:{}/json/list", self.port);
        let res = self.http.get(&url).send().await.map_err(|e| e.to_string())?;
        res.json().await.map_err(|e| e.to_string())
    }

    async fn page_ws(&self) -> Result<String, String> {
        let pages = self.pages().await?;
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
        let (mut ws, _) = connect_async(&ws_url).await.map_err(|e| e.to_string())?;
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
        let result = self
            .cdp_wait(
                "Runtime.evaluate",
                json!({ "expression": expression, "returnByValue": true, "awaitPromise": false }),
                wait,
            )
            .await?;
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
                .eval("(() => { const t = (document.body && document.body.innerText || '').replace(/\\s+/g, ' ').trim(); return t.length > 180 && document.readyState !== 'loading'; })()")
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
        self.arm_teach().await;
        let mut data = self.eval(SNAPSHOT_JS).await?;
        if !data.is_object() {
            data = json!({ "url": "", "title": "", "text": "", "tree": "", "elements": [] });
        }
        if let Ok(shot) = self.screenshot_b64().await {
            data["screenshot_b64"] = json!(shot);
        }
        data["ok"] = json!(true);
        data["ran_on"] = json!("local_computer");
        Ok(data)
    }

    pub async fn screenshot_b64(&self) -> Result<String, String> {
        let result = self.cdp("Page.captureScreenshot", json!({ "format": "png" })).await?;
        result
            .get("data")
            .and_then(Value::as_str)
            .map(str::to_string)
            .ok_or_else(|| "no screenshot".into())
    }

    pub async fn act(&self, actions: &[Value]) -> Result<Value, String> {
        for step in actions {
            let kind = step.get("kind").and_then(Value::as_str).unwrap_or("click");
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
            if kind == "scroll" {
                let dy = step.get("dy").and_then(Value::as_i64).or_else(|| step.get("amount").and_then(Value::as_i64)).unwrap_or(800);
                self.eval(&format!("window.scrollBy(0, {dy})")).await.ok();
                continue;
            }
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
                continue;
            }
            let ref_id = step.get("ref").and_then(Value::as_str).unwrap_or("");
            let text = step.get("text").and_then(Value::as_str).unwrap_or("");
            let js = format!("({ACT_JS})({ref_id:?}, {kind:?}, {text:?})");
            let result = self.eval(&js).await?;
            if result.get("ok").and_then(Value::as_bool) == Some(false) {
                return Err(result.get("error").and_then(Value::as_str).unwrap_or("browser action failed").to_string());
            }
            if result.get("insert").and_then(Value::as_bool) == Some(true) && !text.is_empty() {
                self.type_text(text).await?;
            }
            if result.get("submit").and_then(Value::as_bool) == Some(true) {
                let key = result.get("key").and_then(Value::as_str).unwrap_or("Enter");
                self.key(key).await?;
                tokio::time::sleep(Duration::from_millis(450)).await;
                if key.eq_ignore_ascii_case("enter") || key.eq_ignore_ascii_case("return") {
                    self.eval(SUBMIT_IF_DRAFT_JS).await.ok();
                }
            }
        }
        tokio::time::sleep(Duration::from_millis(700)).await;
        self.snapshot().await
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
