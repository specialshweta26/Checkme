use std::path::PathBuf;
use std::process::Stdio;
use std::time::Duration;

use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use tokio::process::{Child, Command};
use tokio_tungstenite::connect_async;
use tokio_tungstenite::tungstenite::Message;

const SNAPSHOT_JS: &str = include_str!("snapshot.js");
const ACT_JS: &str = include_str!("act.js");
const TEACH_JS: &str = include_str!("teach.js");
const DRAIN_TEACH_JS: &str = r#"(() => { const b = window.__lumioTeachBuf || []; window.__lumioTeachBuf = []; return b.slice(0, 200); })()"#;

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

async fn wait_debug(http: &reqwest::Client, port: u16) -> Result<(), String> {
    let url = format!("http://127.0.0.1:{port}/json/version");
    for _ in 0..40 {
        if http.get(&url).send().await.map(|r| r.status().is_success()).unwrap_or(false) {
            return Ok(());
        }
        tokio::time::sleep(Duration::from_millis(250)).await;
    }
    Err("Chrome did not open its debug port".into())
}

fn http_client() -> Result<reqwest::Client, String> {
    reqwest::Client::builder().timeout(Duration::from_secs(20)).build().map_err(|e| e.to_string())
}

pub async fn attach() -> Result<Chrome, String> {
    let http = http_client()?;
    let port = 9333u16;
    wait_debug(&http, port).await?;
    Ok(Chrome { _child: None, port, http })
}

pub async fn launch(start_url: &str) -> Result<Chrome, String> {
    if !(start_url.starts_with("https://") || start_url.starts_with("http://")) {
        return Err("A full https address is required before opening Chrome.".into());
    }
    let http = http_client()?;
    // Dedicated port + Lumio profile. Never attach to 9222 (that is often the user's own Chrome).
    let port = 9333u16;
    if wait_debug(&http, port).await.is_ok() {
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
    let child = cmd.spawn().map_err(|e| format!("could not start Chrome: {e}"))?;
    wait_debug(&http, port).await?;
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
        let ws_url = self.page_ws().await?;
        let (mut ws, _) = connect_async(&ws_url).await.map_err(|e| e.to_string())?;
        let payload = json!({ "id": 1, "method": method, "params": params });
        ws.send(Message::Text(payload.to_string().into())).await.map_err(|e| e.to_string())?;
        let deadline = tokio::time::Instant::now() + Duration::from_secs(25);
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
        let result = self
            .cdp(
                "Runtime.evaluate",
                json!({ "expression": expression, "returnByValue": true, "awaitPromise": false }),
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
        let budget = wait_ms.max(8000).min(20000);
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
            let js = format!(
                "({ACT_JS})({ref_id:?}, {kind:?}, {text:?})"
            );
            self.eval(&js).await?;
        }
        tokio::time::sleep(Duration::from_millis(400)).await;
        self.snapshot().await
    }

    pub async fn type_text(&self, text: &str) -> Result<(), String> {
        self.cdp("Input.insertText", json!({ "text": text })).await?;
        Ok(())
    }

    pub async fn key(&self, key: &str) -> Result<(), String> {
        self.cdp("Input.dispatchKeyEvent", json!({ "type": "keyDown", "key": key })).await?;
        self.cdp("Input.dispatchKeyEvent", json!({ "type": "keyUp", "key": key })).await?;
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

    pub async fn page_info(&self) -> Result<(String, String, String), String> {
        let shot = self.screenshot_b64().await.unwrap_or_default();
        let url = self.eval("location.href").await.ok().and_then(|v| v.as_str().map(str::to_string)).unwrap_or_default();
        let title = self.eval("document.title").await.ok().and_then(|v| v.as_str().map(str::to_string)).unwrap_or_default();
        Ok((shot, url, title))
    }
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
